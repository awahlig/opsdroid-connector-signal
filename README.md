# opsdroid-connector-signal
opsdroid connector for Signal using [signal-cli-rest-api](https://github.com/bbernhard/signal-cli-rest-api)

## configuration

```yml
connectors:
  signal:
    # URL of this repository for opsdroid to automatically download from.
    repo: https://github.com/awahlig/opsdroid-connector-signal.git

    # URL of the signal-cli-rest-api docker container to connect to.
    url: http://signal-cli-rest-api:8080

    # Phone number that the signal-cli has been registered with.
    number: "+1234567890"

    # Set to true if signal-cli-rest-api is using the json-rpc mode (recommended).
    # See signal-cli-rest-api documentation for more info.
    use-json-rpc: false

    # If not using the json-rpc mode, set how often to poll for new messages (seconds).
    poll-interval: 10
```
