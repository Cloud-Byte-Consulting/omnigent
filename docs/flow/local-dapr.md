# Local Flow Dapr runtime

Flow uses Dapr CLI 1.18.0, Dapr runtime 1.18.1, and
`dapr-ext-workflow` 1.18.0. Docker must be running and the exact Dapr CLI
version must be on `PATH`. The repository contains no credentials.

```console
python -m omnigent.flow.local_dapr init
python -m omnigent.flow.local_dapr start
python -m omnigent.flow.local_dapr status
python -m omnigent.flow.local_dapr history <instance-id>
python -m omnigent.flow.local_dapr stop
```

The app ID is `omnigent-flow`; the Dapr HTTP and gRPC ports are 3510 and
50101. `init` uses Dapr's persistent `dapr_scheduler` Docker volume and
the checked-in Redis actor state store, so stopping and restarting does not
remove workflow history.

To deliberately delete all local Dapr state and initialize a fresh runtime:

```console
python -m omnigent.flow.local_dapr clean-reset --yes
```

This resets the machine-wide self-hosted Dapr installation. The `--yes` guard
is mandatory because the operation is destructive.

With `start` running, the smoke workflow can exercise the operator lifecycle:

```console
dapr workflow run FlowRuntimeSmoke --app-id omnigent-flow --input '{}'
dapr workflow list --app-id omnigent-flow --output json
dapr workflow suspend <instance-id> --app-id omnigent-flow
dapr workflow resume <instance-id> --app-id omnigent-flow
dapr workflow history <instance-id> --app-id omnigent-flow
dapr workflow terminate <instance-id> --app-id omnigent-flow
```
