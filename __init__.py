import logging
import asyncio
import base64
import urllib.parse

import aiohttp
import opsdroid.events
from opsdroid.connector import Connector, register_event


logger = logging.getLogger(__name__)


def encode_group_id(group_id):
    """Encode group_id as returned by the websocket for use with other endpoints."""
    return "group." + base64.b64encode(group_id.encode("ascii")).decode("ascii")


class ConnectorSignal(Connector):
    """A connector for the Signal chat service."""

    def __init__(self, config, *args, **kwargs):
        """Create the connector."""
        super().__init__(config, *args, **kwargs)

        # Parse the connector configuration.
        try:
            self.parsed_url = urllib.parse.urlparse(config["url"])
            self.number = config["bot-number"]
            self.rooms = config.get("rooms", {})
            self.whitelist = frozenset(self.rooms.get(v, v) for v in
                                       config.get("whitelisted-numbers", []))
        except KeyError as error:
            logger.error("required setting '%s' not found", error.args[0])
            raise

        self.inv_rooms = {v: k for k, v in self.rooms.items()}
        self.session = None

    def make_url(self, path_format):
        """Build the url to connect with api.
        Occurrences of {number} in path_format are replaced with the configured
        phone number that the Signal client is using.
        """
        path = path_format.format(number=urllib.parse.quote(self.number))
        return self.parsed_url._replace(path=path).geturl()

    def lookup_target(self, target):
        """Convert room alias into Signal phone number or group ID.
        This is called by constrain_rooms decorator.
        """
        return self.rooms.get(target, target)

    async def connect(self):
        """Connect to the chat service.
        In this case we just create a http session.
        """
        self.session = aiohttp.ClientSession(raise_for_status=True)

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
                        await self.parse_packet(msg.json())
        else:
            interval = self.config.get("poll-interval", 10)
            while True:
                async with self.session.get(url) as resp:
                    packets = await resp.json(content_type=None)
                for packet in packets:
                    await self.parse_packet(packet)
                await asyncio.sleep(interval)

    async def parse_packet(self, packet):
        logger.debug("parse packet %s", packet)
        try:
            envelope = packet["envelope"]
            args = dict(user_id=envelope["sourceNumber"],
                        user=envelope["sourceName"],
                        connector=self,
                        raw_event=packet,
                        event_id=envelope["timestamp"])
        except KeyError as error:
            logger.debug("missing '%s' key", error.args[0])
            return

        if self.whitelist and args["user_id"] not in self.whitelist:
            logger.debug("user '%s' not whitelisted", args["user_id"])
            return

        data_message = envelope.get("dataMessage")
        if data_message:
            await self.parse_data_message(data_message, args)

        typing_message = envelope.get("typingMessage")
        if typing_message:
            await self.parse_typing_message(typing_message, args)

    async def parse_data_message(self, data_message, args):
        try:
            target = encode_group_id(data_message["groupInfo"]["groupId"])
        except KeyError:
            target = args["user_id"]
        args["target"] = self.inv_rooms.get(target, target)

        text = data_message.get("message")
        reaction = data_message.get("reaction")
        if reaction:
            await self.parse_reaction(reaction, args)
        elif text:
            await self.parse_text(text, args)

        attachments = data_message.get("attachments")
        for attachment in (attachments or ()):
            await self.parse_attachment(attachment, args)

    async def parse_reaction(self, reaction, args):
        emoji = ("" if reaction.get("isRemove") else reaction["emoji"])
        linked = opsdroid.events.Event(user_id=reaction["targetAuthorNumber"],
                                       target=args["target"],
                                       connector=args["connector"],
                                       event_id=reaction["targetSentTimestamp"])
        event = opsdroid.events.Reaction(emoji=emoji, linked_event=linked, **args)
        logger.info("received reaction %s from %s", event, event.target)
        await self.opsdroid.parse(event)

    async def parse_text(self, text, args):
        event = opsdroid.events.Message(text=text, **args)
        logger.info("received message %s from %s", event, event.target)
        await self.opsdroid.parse(event)

    async def parse_attachment(self, attachment, args):
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

    async def parse_typing_message(self, typing_message, args):
        try:
            target = encode_group_id(typing_message["groupId"])
        except KeyError:
            target = args["user_id"]
        args["target"] = self.inv_rooms.get(target, target)

        trigger = (typing_message.get("action") == "STARTED")
        user_id = args.pop("user_id")
        event = opsdroid.events.Typing(trigger=trigger,
                                       timeout=15,
                                       **args)
        event.user_id = user_id  # here before pr#1877 lands
        logger.info("received typing %s from %s", event, event.target)
        await self.opsdroid.parse(event)

    def get_recipients_from_event(self, event):
        """Get Signal recipients from an opsdroid Event object."""
        return [x for x in (self.lookup_target(event.target),) if x]

    @register_event(opsdroid.events.Message)
    async def send_message(self, event):
        """Send a text message."""
        logger.info("send message %s to %s", event, event.target)
        data = {
            "number": self.number,
            "recipients": self.get_recipients_from_event(event),
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
            "recipients": self.get_recipients_from_event(event),
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
        method = (self.session.put if event.trigger else self.session.delete)
        url = self.make_url("/v1/typing-indicator/{number}")
        data = {"recipient": self.get_recipients_from_event(event)[0]}
        async with method(url, json=data) as resp:
            await resp.json(content_type=None)

    @register_event(opsdroid.events.Reaction)
    async def send_reaction(self, event):
        """Send a reaction to a message."""
        logger.info("send reaction %s to %s", event, event.target)
        method = (self.session.post if event.emoji else self.session.delete)
        url = self.make_url("/v1/reactions/{number}")
        data = {
            "reaction": event.emoji,
            "recipient": self.get_recipients_from_event(event)[0],
            "target_author": event.linked_event.user_id,
            "timestamp": event.linked_event.event_id,
        }
        async with method(url, json=data) as resp:
            await resp.json(content_type=None)
