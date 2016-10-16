import telebot
import logging

mapped_content_type = {
	"text": "text",
	"location": "location",
	"contact": "", # TODO
	"new_chat_member": "user_joined",
	"left_chat_member": "user_left",
	"new_chat_title": "ctitle_changed",
	"delete_chat_photo": "",
	"group_chat_created": "", # can't occurr because we're a bot
	"supergroup_chat_created": "", # can't occurr because we're a bot
	"channel_chat_created": "", # can't occurr because we're a bot
	"migrate_to_chat_id": "", # FIXME: can we safely ignore these?
	"migrate_from_chat_id": "", # FIXME: can we safely ignore these?
	"pinned_message": "cpinned_changed",
}

content_types_media = [
	"audio",
	"document",
	"photo",
	"sticker",
	"video",
	"voice",
]

def ostr(obj):
	if obj is None:
		return ""
	return obj

class TelegramMediaContainer():
	def __init__(self, orig, init_from="event"):
		if init_from == "photo_list":
			self.type = "photo"
			c = sorted(orig, key=lambda e: e.width*e.height, reverse=True)[0]
			self.dimensions = (c.width, c.height)
			self.file_id = c.file_id
			self.file_size = c.file_size
			return
		elif init_from == "event":
			pass # see below
		else:
			raise NotImplementedException("???")

		self.type = orig.content_type
		if self.type == "audio":
			c = orig.audio
			self.mime = c.mime_type
			self.duration = c.duration
			if c.performer is None and c.title is None:
				self.desc = None
			elif c.performer is None or c.title is None:
				self.desc = ostr(c.performer) + ostr(c.title)
			else:
				self.desc = "%s – %s" % (c.performer, c.title)
		elif self.type == "document":
			c = orig.document
			self.mime = c.mime_type
		elif self.type == "photo":
			c = sorted(orig.photo, key=lambda e: e.width*e.height, reverse=True)[0]
			self.dimensions = (c.width, c.height)
		elif self.type == "sticker":
			c = orig.sticker
			self.emoji = c.emoji
			self.dimensions = (c.width, c.height)
		elif self.type == "voice":
			c = orig.voice
			self.duration = c.duration
		else:
			# TODO: video
			raise NotImplementedException("content type not supported")

		self.file_id = c.file_id
		self.file_size = c.file_size

class TelegramClient():
	def __init__(self, config):
		if config["token"] == "":
			logging.error("No telegram token specified, exiting")
			exit(1)
		self.token = config["token"]
		self.bot = telebot.TeleBot(self.token)
		self.event_handlers = {}

		self._telebot_event_handler(self.cmd_start, commands=["start"])
		self._telebot_event_handler(self.cmd_help, commands=["help"])
		self._telebot_event_handler(self.cmd_me, commands=["me"]) # not portable
		for k, v in mapped_content_type.items():
			if v == "": continue
			self._telebot_event_handler_passthrough(v, content_types=[k])
		for k in content_types_media:
			self._telebot_event_handler(self.on_media, content_types=[k])
		self._telebot_event_handler(self.on_new_chat_photo, content_types=["new_chat_photo"])

	def run(self):
		logging.info("Polling for Telegram events")
		self.bot.polling()

	def event_handler(self, name, func):
		self.event_handlers[name] = func


	def _invoke_event_handler(self, name, args=(), kwargs={}):
		if name not in self.event_handlers.keys():
			logging.warning("Unhandeled '%s' event", name)
			return
		try:
			self.event_handlers[name](*args, **kwargs)
		except Exception as e:
			logging.exception("Exception in Telegram event handler")

	def _telebot_event_handler(self, _func, **kwargs):
		self.bot.message_handler(**kwargs)(_func)

	def _telebot_event_handler_passthrough(self, evname, **kwargs):
		def h(message):
			self._invoke_event_handler(evname, (message, ))
		self._telebot_event_handler(h, **kwargs)


	def cmd_start(self, message):
		self._invoke_event_handler("cmd_start", (message, ))

	def cmd_help(self, message):
		self._invoke_event_handler("cmd_help", (message, ))

	def cmd_me(self, message):
		self._invoke_event_handler("cmd_me", (message, ))

	def on_media(self, message):
		self._invoke_event_handler("media", (message, TelegramMediaContainer(message)))

	def on_new_chat_photo(self, message):
		if message.delete_chat_photo:
			media = None
		else:
			media = TelegramMediaContainer(message.new_chat_photo, init_from="photo_list")
		self._invoke_event_handler("cphoto_changed", (message, media))


	def send_message(self, chat_id, text, **kwargs):
		self.bot.send_message(chat_id, text, **kwargs)

	def send_reply_message(self, event, text, **kwargs):
		self.bot.send_message(event.chat.id, text, reply_to_message_id=event.message_id)

	def get_file_url(self, file_id):
		info = self.bot.get_file(file_id)
		return "https://api.telegram.org/file/bot%s/%s" % (self.token, info.file_path)