import logging
import asyncio
import base64
import urllib.parse

import aiohttp
import opsdroid.events
from opsdroid.connector import Connector, register_event


logger = logging.getLogger(__name__)


def encode_group_id(group_id):
    # Encode group_id as returned by the websocket for use with other endpoints.
    return "group." + base64.b64encode(group_id.encode("ascii")).decode("ascii")


class ConnectorSignal(Connector):
    """A connector for the Signal chat service."""

    def __init__(self, *args, **kwargs):
        """Create the connector."""
        super().__init__(*args, **kwargs)
        self.session = None
        self.parsed_url = urllib.parse.urlparse(self.get_required_setting("url"))
        self.number = self.get_required_setting("number")

    def get_required_setting(self, key):
        """Get a required setting from the config.
        Log an error if not configured.
        """
        try:
            return self.configuration[key]
        except KeyError:
            logger.error("required setting '%s' not found in configuration.yml", key)
            raise

    def make_url(self, path_format):
        """Build the url to connect with api.
        Occurrences of {number} in path_format are replaced with the configured
        phone number that the Signal client is using.
        """
        path = path_format.format(number=urllib.parse.quote(self.number))
        return urllib.parse.urlunparse(self.parsed_url._replace(path=path))

    async def connect(self):
        """Connect to the chat service.
        In this case we just create a http session.
        """
        self.session = aiohttp.ClientSession()

    async def disconnect(self):
        """Disconnect from the chat service."""
        await self.session.close()
        self.session = None

    async def listen(self):
        """Listen for and parse new messages."""
        async with self.session.get(self.make_url("/v1/about")) as resp:
            about = await resp.json()
        logger.debug("about signal-cli-rest-api %s", about)

        url = self.make_url("/v1/receive/{number}")
        if about.get("mode") == "json-rpc":
            async with self.session.ws_connect(url) as ws:
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        await self.handle_packet(msg.json())
        else:
            interval = self.configuration.get("poll-interval", 10)
            while True:
                async with self.session.get(url) as resp:
                    packets = await resp.json(content_type=None)
                for packet in packets:
                    await self.handle_packet(packet)
                await asyncio.sleep(interval)

    async def handle_packet(self, packet):
        logger.debug("handle packet %s", packet)
        try:
            envelope = packet["envelope"]
            args = dict(user_id=envelope["sourceNumber"],
                        user=envelope["sourceName"],
                        connector=self,
                        raw_event=packet,
                        event_id=envelope["timestamp"])
        except KeyError:
            return

        data_message = envelope.get("dataMessage")
        if data_message:
            await self.handle_data_message(data_message, args)

        typing_message = envelope.get("typingMessage")
        if typing_message:
            await self.handle_typing_message(typing_message, args)

    async def handle_data_message(self, data_message, args):
        try:
            args["target"] = encode_group_id(data_message["groupInfo"]["groupId"])
        except KeyError:
            args["target"] = args["user_id"]

        text = data_message.get("message")
        reaction = data_message.get("reaction")
        if reaction:
            await self.handle_reaction(reaction, args)
        elif text:
            await self.handle_text(text, args)

        attachments = data_message.get("attachments")
        for attachment in (attachments or ()):
            await self.handle_attachment(attachment, args)

    async def handle_reaction(self, reaction, args):
        emoji = ("" if reaction.get("isRemove") else reaction["emoji"])
        linked = opsdroid.events.Event(user_id=reaction["targetAuthorNumber"],
                                       target=args["target"],
                                       connector=args["connector"],
                                       event_id=reaction["targetSentTimestamp"])
        event = opsdroid.events.Reaction(emoji=emoji, linked_event=linked, **args)
        logger.info("received reaction %s from %s", event, event.target)
        await self.opsdroid.parse(event)

    async def handle_text(self, text, args):
        event = opsdroid.events.Message(text=text, **args)
        logger.info("received message %s from %s", event, event.target)
        await self.opsdroid.parse(event)

    async def handle_attachment(self, attachment, args):
        url = self.make_url(f"v1/attachments/{attachment['id']}")
        name = attachment.get("filename")
        mimetype = attachment.get("contentType")
        file_type = (mimetype or "").split("/")[0]

        event_class = {
            "image": opsdroid.events.Image,
            "video": opsdroid.events.Video,
        }.get(file_type, opsdroid.events.File)

        event = event_class(url=url,
                            name=name,
                            mimetype=mimetype,
                            **args)
        logger.info("received file %s from %s", event, event.target)
        await self.opsdroid.parse(event)

    async def handle_typing_message(self, typing_message, args):
        try:
            args["target"] = encode_group_id(typing_message["groupId"])
        except KeyError:
            args["target"] = args["user_id"]

        trigger = (typing_message.get("action") == "STARTED")
        user_id = args.pop("user_id")
        event = opsdroid.events.Typing(trigger=trigger,
                                       timeout=15,
                                       **args)
        event.user_id = user_id  # here before pr#1877 lands
        logger.info("received typing %s from %s", event, event.target)
        await self.opsdroid.parse(event)

    @register_event(opsdroid.events.Message)
    async def send_message(self, event):
        """Send a text message."""
        logger.info("send message %s to %s", event, event.target)
        data = {
            "number": self.number,
            "recipients": [event.target],
            "message": event.text,
        }
        async with self.session.post(self.make_url("/v2/send"), json=data) as resp:
            result = await resp.json()
        logger.debug("result %s", result)

    @register_event(opsdroid.events.File, include_subclasses=True)
    async def send_file(self, event):
        """Send a file/image/video message."""
        logger.info("send file %s to %s", event, event.target)
        file_bytes = await event.get_file_bytes()
        data = {
            "number": self.number,
            "recipients": [event.target],
            "base64_attachments": [
                base64.b64encode(file_bytes).decode("ascii"),
            ],
        }
        async with self.session.post(self.make_url("/v2/send"), json=data) as resp:
            result = await resp.json()
        logger.debug("result %s", result)

    @register_event(opsdroid.events.Typing)
    async def send_typing(self, event):
        """Set or remove the typing indicator."""
        logger.info("send typing %s to %s", event, event.target)
        data = {"recipient": event.target}
        method = (self.session.put if event.trigger else self.session.delete)
        async with method(self.make_url("/v1/typing-indicator/{number}")) as resp:
            await resp.read()

    @register_event(opsdroid.events.Reaction)
    async def send_reaction(self, event):
        """Send a reaction to a message."""
        logger.info("send reaction %s to %s", event, event.target)
        data = {
            "reaction": event.emoji,
            "recipient": event.target,
            "target_author": event.linked_event.user_id,
            "timestamp": event.linked_event.event_id,
        }
        method = (self.session.post if event.emoji else self.session.delete)
        async with method(self.make_url("/v1/reactions/{number}"), json=data) as resp:
            await resp.read()
