from omnigent.flow.providers import (
    AdapterRegistration,
    AdapterRequest,
    AdapterResponse,
    NodeExecutionRequest,
    NodeExecutionSuccess,
    ProviderCapabilities,
    ProviderRegistry,
    ProviderRouter,
    TokenUsage,
)


class RecordingAdapter:
    def __init__(self) -> None:
        self.requests: list[AdapterRequest] = []
        self.credentials: list[str] = []

    async def execute(self, request: AdapterRequest, *, credential: str) -> AdapterResponse:
        self.requests.append(request)
        self.credentials.append(credential)
        return AdapterResponse(
            output={"summary": "done"},
            usage=TokenUsage(input_tokens=8, output_tokens=3, total_tokens=11),
            latency_ms=25,
            warnings=("fake warning",),
        )


async def test_router_adapter_boundary_preserves_provider_neutral_contract() -> None:
    adapter = RecordingAdapter()
    router = ProviderRouter(
        ProviderRegistry(
            [
                AdapterRegistration(
                    provider="fake",
                    models=frozenset({"alpha"}),
                    credential_reference="provider-key",
                    capabilities=ProviderCapabilities(
                        tools=True,
                        structured_output=True,
                        usage_reporting=True,
                    ),
                    enabled=True,
                    adapter=adapter,
                )
            ]
        ),
        credentials={"provider-key": "secret"},
    )
    node_request = NodeExecutionRequest(
        run_id="run-1",
        node_id="summarize",
        instructions="Summarize dependencies",
        model="fake:alpha",
        default_model=None,
        allowed_tools=("search",),
        dependency_outputs={"collect": {"facts": [1, 2]}},
        output_schema={"type": "object", "required": ["summary"]},
        remaining_token_budget=90,
        attempt=2,
    )

    result = await router.execute(node_request)

    assert result == NodeExecutionSuccess(
        output={"summary": "done"},
        provider="fake",
        model="alpha",
        usage=TokenUsage(input_tokens=8, output_tokens=3, total_tokens=11),
        latency_ms=25,
        attempt=2,
        warnings=("fake warning",),
    )
    assert adapter.requests == [
        AdapterRequest(
            run_id="run-1",
            node_id="summarize",
            instructions="Summarize dependencies",
            model="alpha",
            allowed_tools=("search",),
            dependency_outputs={"collect": {"facts": [1, 2]}},
            output_schema={"type": "object", "required": ["summary"]},
            remaining_token_budget=90,
            attempt=2,
        )
    ]
    assert adapter.credentials == ["secret"]
