# Provider routing

Flow routes each node through one provider-neutral Python boundary. A node's
explicit `provider:model` wins over the run's `defaultModel`. The immutable
registry snapshot resolves that reference to an enabled adapter, verifies its
tool and structured-output capabilities, and resolves its credential reference
before invoking the adapter exactly once.

Credentials are configuration inputs, not contract fields. They are passed only
to the selected adapter and never appear in normalized results or safe provider
failures. Adapters translate an expected provider failure to
`ProviderAdapterError`; unexpected programming errors are not hidden.

```text
NodeExecutionRequest
        |
        v
model selection -> registry -> capability + credential checks -> adapter
        |              |                    |                    |
        +--------------+--------------------+--------------------+
                               |
                               v
             NodeExecutionSuccess | NodeExecutionFailure
```

## Acceptance coverage

| Gherkin scenario | Automated coverage |
| --- | --- |
| Route an explicit model | `test_explicit_model_takes_precedence_and_records_selection` |
| Use the default model | `test_default_model_is_used_when_node_omits_model` |
| Reject invalid configuration before invocation | `test_rejects_invalid_configuration_before_invocation` |
| Protect secrets | `test_adapter_exception_is_safe_and_does_not_expose_credential` and the router-adapter integration test |
| Route deterministically | `test_routing_is_stable_for_a_configuration_snapshot` |

Provider-specific SDKs, retry classification, structured-output enforcement,
and token-budget policy are intentionally left to their dedicated Flow work
items. Fake adapters exercise this issue's complete integration boundary.
