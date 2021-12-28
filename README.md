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
    bot-number: "+1234567890"

    # Optional aliases for Signal phone numbers and group IDs.
    # Makes working with some skills easier.
    rooms:
      "alias": "+2134567890"
      "general": "group.RVZ5..."

    # Optional list of Signal phone numbers that can talk to the bot.
    # If not empty, numbers that are not on the list are ignored.
    whitelisted-numbers:
      - "+3214567890"
      - "alias"

    # How often to poll for new messages, in seconds.
    # This is ignored if signal-cli-rest-api is using the json-rpc mode (recommended),
    # where polling is not needed.  See signal-cli-rest-api documentation for more info.
    poll-interval: 10
```
