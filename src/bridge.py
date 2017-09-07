import re
import logging
from collections import namedtuple

def dump(obj, name=None, r=False): ##DEBUG##
	name = "" if name is None else (name + ".")
	for e, ev in ((e, getattr(obj, e)) for e in dir(obj)):
		if e.startswith("_") or ev is None:
			continue
		if r and ev.__class__.__name__[0].isupper():
			print("%s%s (%s)" % (name, e, ev.__class__.__name__))
			dump(ev, name + e, r)
		else:
			print("%s%s = %r" % (name, e, ev))

def format_duration(d):
	m, s = divmod(d, 60)
	if m == 0:
		return "%ds" % s
	elif m > 0 and s == 0:
		return "%dm" % m
	return "%dm%ds" % (m, s)

class NickColorizer():
	def __init__(self, config=None):
		if config is None:
			self.colors = [2, 3, 4, 6, 7, 8, 9, 10, 11, 12]
		else:
			self.colors = list(config)
	@staticmethod
	def _hash(s):
		v = 0
		state = 0x34343434
		for c in s:
			c = ord(c)
			v += ((c >> 4) ^ (c >> 2)) + state
			state = (state + 0x79B99E37) & 0xffffffff
			v += ((c >> 1) ^ c) + state
		return v
	def enabled(self):
		return len(self.colors) != 0
	def colorize(self, s):
		if not self.enabled(): # disabled
			return s
		color = NickColorizer._hash(s) % len(self.colors)
		color = self.colors[color]
		return "\x03%02d%s\x0f" % (color, s)

class IRCFormattingConverter(): # IRC -> HTML
	def __init__(self, enabled):
		self.enabled = enabled
		tmp = namedtuple("StyleCombo", ["open", "close"])
		if self.enabled:
			self.bold = tmp(open="<b>", close="</b>")
			self.italics = tmp(open="<i>", close="</i>")
		else:
			self.bold = self.italics = tmp(open="", close="")
	def convert(self, text):
		bold, italics = False, False
		skip_digits = 0
		ret = ""
		for c in text:
			if skip_digits > 0:
				skip_digits = (skip_digits - 1) if c.isdigit() else 0
				if c.isdigit():
					continue
			# Handle styles
			if c == "\x02":
				ret += self.bold.close if bold else self.bold.open
				bold = not bold
			elif c == "\x03": # color (ignored)
				skip_digits = 2
			elif c == "\x0f": # reset
				if bold:
					ret += self.bold.close
				if italics:
					ret += self.italics.close
				bold, italics = False, False
			elif c == "\x1d":
				ret += self.italics.close if italics else self.italics.open
				italics = not italics
			elif c == "\x1f": # underline (ignored)
				pass
			else:
				# Handle text: escape if required and pass through
				if c in ("<", ">", "&"):
					c = "&#" + str(ord(c)) + ";"
				ret += c
		if bold:
			ret += self.bold.close
		if italics:
			ret += self.italics.close
		return ret

class TelegramFormattingConverter(): # Telegram -> IRC
	def __init__(self, enabled, userfmt):
		self.enabled = enabled
		self.userfmt = userfmt
	def convert(self, text, entities):
		_enc = "utf-16-le" # need to specify endianness to avoid a BOM
		_filt = lambda text: text.replace("\n", " … ")
		if not self.enabled or entities is None:
			return _filt(text)
		# Telegrams entities are positioned in units of UTF-16 code points
		text = text.encode(_enc)
		tpos = 0
		ret = ""
		while tpos < len(text):
			e = next((e for e in entities if e.offset >= (tpos>>1)), None)
			if e is None:
				# Ordinary text before end of msg
				rtext = text[tpos:]
			elif tpos < (e.offset<<1):
				# Ordinary text before next entity
				rtext = text[tpos:(e.offset<<1)]
				e = None

			if e is None:
				# No entity / no special handling
				ret += _filt(rtext.decode(_enc))
				tpos += len(rtext)
				continue
			# Handle entity
			rlen = e.length << 1
			etext = _filt(text[tpos:tpos+rlen].decode("utf16"))
			if e.type == "mention":
				u = namedtuple("FakeUser", ["username", "first_name", "last_name"])(
					username=etext[1:],
					first_name=None,
					last_name=None,
				)
				ret += "@" + self.userfmt(u)
			elif e.type == "code" or e.type == "pre":
				c = 15 # FIXME should be configurable
				ret += "\x03%02d" % c + etext + "\x0f"
			elif e.type == "bold":
				ret += "\x02" + etext + "\x02"
			elif e.type == "italic":
				ret += "\x1d" + etext + "\x1d"
			elif e.type == "text_mention":
				ret += self.userfmt(e.user)
			else: # unhandled: hashtag, bot_command, url, email, text_link
				ret += etext
			tpos += rlen
		return ret

LinkTuple = namedtuple("LinkTuple", ["telegram", "irc"])
config_names = [
	"telegram_bold_nicks",
	"irc_nick_colors",

	"forward_sticker_dimensions",
	"forward_sticker_emoji",
	"forward_document_mime",
	"forward_audio_description",
	"forward_text_formatting_irc",
	"forward_text_formatting_telegram",
	"forward_joinleave_irc",
	"forward_joinleave_telegram"
]

class Bridge():
	def __init__(self, tg, irc, wb, config):
		self.tg = tg
		self.irc = irc
		self.web = wb
		#
		self.links = set(LinkTuple(**e) for e in config["links"])
		logging.info("%d link(s) configured", len(self.links))
		if "irc_nick_colors" not in config["options"].keys():
			config["options"]["irc_nick_colors"] = None # use default
		self.conf = namedtuple("Conf", config_names)(**config["options"])
		#
		self.nc = NickColorizer(self.conf.irc_nick_colors)
		self.tf = namedtuple("T", ["irc", "tg"])(
			irc=IRCFormattingConverter(self.conf.forward_text_formatting_irc),
			tg=TelegramFormattingConverter(self.conf.forward_text_formatting_telegram, self._tg_format_user),
		)
		self.file_number = 1 # Downside: can repeat if files are rare/you restart often

		self.irc.event_handler("connected", self.irc_connected)
		self._irc_event_handler("message", self.irc_message)
		self._irc_event_handler("action", self.irc_action)
		self._irc_event_handler("join", self.irc_join)
		self._irc_event_handler("part", self.irc_part)
		self._irc_event_handler("kick", self.irc_kick)
		self.tg.event_handler("cmd_help", self.tg_help)
		self._tg_event_handler("cmd_me", self.tg_me)
		self._tg_event_handler("text", self.tg_text)
		self._tg_event_handler("media", self.tg_media)
		self._tg_event_handler("location", self.tg_location)
		self._tg_event_handler("contact", self.tg_contact)
		self._tg_event_handler("game", self.tg_game)
		self._tg_event_handler("users_joined", self.tg_users_joined)
		self._tg_event_handler("user_left", self.tg_user_left)
		self._tg_event_handler("ctitle_changed", self.tg_ctitle_changed)
		self._tg_event_handler("cphoto_changed", self.tg_cphoto_changed)
		self._tg_event_handler("cphoto_deleted", self.tg_cphoto_deleted)
		self._tg_event_handler("cpinned_changed", self.tg_cpinned_changed)

	def _irc_event_handler(self, event, func):
		# So we don't have to repeat this code in every handler
		def wrap(event, *args):
			if event.channel is None:
				return
			l = self._find_link(irc=event)
			if l is None:
				logging.warning("IRC channel %s is not linked to anywhere", event.channel)
				return
			func(l, event, *args)
		self.irc.event_handler(event, wrap)

	def _tg_event_handler(self, event, func):
		# So we don't have to repeat this code in every handler
		def wrap(event, *args):
			if event.chat.type in ("private", "channel"):
				return
			l = self._find_link(tg=event)
			if l is None:
				logging.warning("Telegram chat %d is not linked to anywhere", event.chat.id)
				return
			func(l, event, *args)
		self.tg.event_handler(event, wrap)

	def _find_link(self, tg=None, irc=None):
		if tg is not None:
			for l in self.links:
				if l.telegram == tg.chat.id:
					return l
			return None
		elif irc is not None:
			for l in self.links:
				if l.irc == irc.channel:
					return l
			return None
		raise NotImplementedException()

	def _tg_format_user(self, user):
		if user.username is not None:
			return self.nc.colorize(user.username)
		v1 = user.first_name
		v2 = user.last_name
		if v1 == "": # (can't be None)
			italics = "\x1d" if self.nc.enabled() else ""
			return italics + "Deleted Account" + italics
		return self.nc.colorize( v1 + ("" if v2 is None else " " + v2) )

	def _tg_format_msg_prefix(self, event, action=False):
		fmt = "* %s" if action else "<%s>"
		r = fmt % self._tg_format_user(event.from_user)
		if event.reply_to_message is not None and not action:
			if event.reply_to_message.from_user.id == self.tg.get_own_user().id:
				m = re.match(r"(?:<([^>]+)>|\* ([^ ]+)) ", event.reply_to_message.text or "")
				if m:
					# i don't understand why this happens
					r += " %s," % (m.group(1) or m.group(2))
				else:
					logging.warning("Failed to parse our own message: %r", event.reply_to_message.text)
			else:
				r += " @%s," % self._tg_format_user(event.reply_to_message.from_user)
		if event.forward_from is not None:
			r += " Fwd from %s:" % self._tg_format_user(event.forward_from)
		elif event.forward_from_chat is not None:
			r += " Fwd from %s:" % event.forward_from_chat.title
		return r

	def _tg_format_msg(self, event):
		# TODO: move code for media messages here (for media in pinned messages)
		pre = self._tg_format_msg_prefix(event) + " "
		if event.content_type != "text":
			return pre + "(Media message)"
		return pre + self.tf.tg.convert(event.text, event.entities)


	def irc_connected(self):
		for l in self.links:
			self.irc.join(l.irc)

	def irc_message(self, l, event):
		logging.info("[IRC] %s in %s says: %s", event.nick, event.channel, event.message)
		if self.conf.telegram_bold_nicks:
			fmt = "&lt;<b>%s</b>&gt; %s"
		else:
			fmt = "&lt;%s&gt; %s"
		msg = fmt % (event.nick, self.tf.irc.convert(event.message))
		self.tg.send_message(l.telegram, msg, parse_mode="HTML")

	def irc_action(self, l, event):
		logging.info("[IRC] %s in %s does action: %s", event.nick, event.channel, event.message)
		if self.conf.telegram_bold_nicks:
			fmt = "* <b>%s</b> %s"
		else:
			fmt = "* %s %s"
		msg = fmt % (event.nick, self.tf.irc.convert(event.message))
		self.tg.send_message(l.telegram, msg, parse_mode="HTML")

	def irc_join(self, l, event):
		logging.info("[IRC] %s joins %s", event.nick, event.channel)
		if not self.conf.forward_joinleave_irc:
			return
		if self.conf.telegram_bold_nicks:
			fmt = "<b>%s</b> has joined"
		else:
			fmt = "%s has joined"
		self.tg.send_message(l.telegram, fmt % event.nick, parse_mode="HTML")

	def irc_part(self, l, event):
		logging.info("[IRC] %s leaves %s", event.nick, event.channel)
		if not self.conf.forward_joinleave_irc:
			return
		if self.conf.telegram_bold_nicks:
			fmt = "<b>%s</b> has left"
		else:
			fmt = "%s has left"
		self.tg.send_message(l.telegram, fmt % event.nick, parse_mode="HTML")

	def irc_kick(self, l, event):
		logging.info("[IRC] %s kicks %s", event.nick, event.othernick)
		if self.conf.telegram_bold_nicks:
			fmt = "<b>%s</b> was kicked by <b>%s</b>"
		else:
			fmt = "%s was kicked by %s"
		self.tg.send_message(l.telegram, fmt % (event.othernick, event.nick), parse_mode="HTML")


	def tg_help(self, event):
		self.tg.send_reply_message(event, "pytgbridge (Telegram)")

	def tg_me(self, l, event):
		if len(event.text.split(" ")) < 2:
			return
		# TODO consider supporting formatting here
		atext = " ".join(event.text.split(" ")[1:])
		logging.info("[TG] /me action: %s", atext)
		self.irc.privmsg(l.irc, self._tg_format_msg_prefix(event, True) + " " + atext)

	def tg_text(self, l, event):
		logging.info("[TG] text: %s", event.text)
		self.irc.privmsg(l.irc, self._tg_format_msg(event))

	def tg_media(self, l, event, media):
		logging.info("[TG] media (%s)", media.type)
		mediadesc = "(???)"
		mediaextension = None # only needed if it differs from the default
		if media.type == "audio":
			if media.desc is not None and self.conf.forward_audio_description:
				mediadesc = "(Audio, %s: %s)" % (format_duration(media.duration), media.desc)
			else:
				mediadesc = "(Audio, %s)" % format_duration(media.duration)
		elif media.type == "document":
			if self.conf.forward_document_mime:
				mediadesc = "(Document, %s)" % media.mime
			else:
				mediadesc = "(Document)"
		elif media.type == "photo":
			mediadesc = "(Photo, %dx%d)" % media.dimensions
		elif media.type == "sticker":
			if self.conf.forward_sticker_dimensions:
				mediadesc = "(Sticker, %dx%d)" % media.dimensions
			else:
				mediadesc = "(Sticker)"
			if self.conf.forward_sticker_emoji:
				mediadesc += " " + media.emoji
		elif media.type == "video":
			mediadesc = "(Video, %s)" % format_duration(media.duration)
		elif media.type == "video_note":
			mediadesc = "(Video Note, %s)" % format_duration(media.duration)
		elif media.type == "voice":
			mediadesc = "(Voice, %s)" % format_duration(media.duration)
			# use .ogg instead of .oga as browsers don't play it otherwise
			if self.tg.get_file_url(media.file_id).endswith(".oga"):
				mediaextension = "ogg"
		#
		if mediaextension is None:
			mediaextension = self.tg.get_file_url(media.file_id).split(".")[-1]
		mediafilename = "file_%d.%s" % (self.file_number, mediaextension)
		self.file_number += 1
		url = self.web.download_and_serve(self.tg.get_file_url(media.file_id), filename=mediafilename)
		post = (" " + event.caption) if event.caption is not None else ""
		post = post.replace("\n", " … ")
		self.irc.privmsg(l.irc, self._tg_format_msg_prefix(event) + " " + mediadesc + " " + url + post)

	def tg_location(self, l, event):
		logging.info("[TG] location")
		self.irc.privmsg(l.irc, "%s (Location, lat: %.4f, lon: %.4f)" % (
			self._tg_format_msg_prefix(event),
			event.location.longitude,
			event.location.latitude,
		))

	def tg_contact(self, l, event):
		logging.info("[TG] contact")
		self.irc.privmsg(l.irc, "%s (Contact, Name: %s%s, Phone: %s)" % (
			self._tg_format_msg_prefix(event),
			event.contact.first_name,
			(" " + event.contact.last_name) if event.contact.last_name is not None else "",
			event.contact.phone_number,
		))

	def tg_game(self, l, event):
		logging.info("[TG] game")
		gamedesc = "\"%s\"" % event.game.title
		if event.game.description is not None:
			gamedesc += ": " + event.game.description
		self.irc.privmsg(l.irc, "%s (Game, %s)" % (self._tg_format_msg_prefix(event), gamedesc))

	def tg_users_joined(self, l, event):
		if not self.conf.forward_joinleave_telegram:
			return
		for member in event.new_chat_members:
			logging.info("[TG] user joined: %d", member.id)
			if event.from_user.id == member.id:
				self.irc.privmsg(l.irc, "%s has joined" % self._tg_format_user(member))
			else:
				self.irc.privmsg(l.irc, "%s was added by %s" % (
					self._tg_format_user(member),
					self._tg_format_user(event.from_user),
				))

	def tg_user_left(self, l, event):
		logging.info("[TG] user left: %d", event.left_chat_member.id)
		if not self.conf.forward_joinleave_telegram:
			return
		if event.from_user.id == event.left_chat_member.id:
			self.irc.privmsg(l.irc, "%s has left" % self._tg_format_user(event.from_user))
		else:
			self.irc.privmsg(l.irc, "%s was removed by %s" % (
				self._tg_format_user(event.left_chat_member),
				self._tg_format_user(event.from_user),
			))

	def tg_ctitle_changed(self, l, event):
		logging.info("[TG] chat title changed: %s", event.new_chat_title)
		self.irc.privmsg(l.irc, "%s set a new chat title: %s" % (
			self._tg_format_user(event.from_user),
			event.new_chat_title,
		))

	def tg_cphoto_changed(self, l, event, media):
		logging.info("[TG] chat photo changed")
		url = self.web.download_and_serve(self.tg.get_file_url(media.file_id))
		self.irc.privmsg(l.irc, "%s set a new chat photo (%dx%d): %s" % (
			self._tg_format_user(event.from_user),
			media.dimensions[0], media.dimensions[1], url
		))

	def tg_cphoto_deleted(self, l, event):
		logging.info("[TG] chat photo deleted")
		self.irc.privmsg(l.irc, "%s deleted the chat photo" % (
			self._tg_format_user(event.from_user),
		))

	def tg_cpinned_changed(self, l, event):
		logging.info("[TG] pinned message changed")
		self.irc.privmsg(l.irc, "%s pinned message: %s" % (
			self._tg_format_user(event.from_user),
			self._tg_format_msg(event.pinned_message),
		))

