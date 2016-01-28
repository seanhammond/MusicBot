import time
import logging
import inspect
import traceback
import asyncio
import aiohttp
import discord
import dataIO
import win_unicode_console
from random import choice

from discord import utils
from discord.enums import ChannelType
from discord.object import Object
from discord.voice_client import VoiceClient

from musicbot.config import Config
from musicbot.player import MusicPlayer
from musicbot.playlist import Playlist
from musicbot.utils import load_file, extract_user_id, write_file

from .downloader import extract_info
from .exceptions import CommandError
from .constants import DISCORD_MSG_CHAR_LIMIT
from .opus_loader import load_opus_lib

VERSION = '2.0'

load_opus_lib()


class SkipState(object):
    def __init__(self):
        self.skippers = set()

    @property
    def skip_count(self):
        return len(self.skippers)

    def reset(self):
        self.skippers.clear()

    def add_skipper(self, skipper):
        self.skippers.add(skipper)
        return self.skip_count


class Response(object):
    def __init__(self, content, reply=False, delete_after=0):
        self.content = content
        self.reply = reply
        self.delete_after = delete_after


class MusicBot(discord.Client):
    def __init__(self, config_file='config/options.txt'):
        super().__init__()

        self.players = {}
        self.voice_clients = {}
        self.voice_client_connect_lock = asyncio.Lock()
        self.config = Config(config_file)

        self.blacklist = set(map(int, load_file(self.config.blacklist_file)))
        self.whitelist = set(map(int, load_file(self.config.whitelist_file)))
        self.backuplist = load_file(self.config.backup_playlist_file)

    async def get_voice_client(self, channel):
        if isinstance(channel, Object):
            channel = self.get_channel(channel.id)

        if getattr(channel, 'type', ChannelType.text) != ChannelType.voice:
            raise AttributeError('Channel passed must be a voice channel')

        with await self.voice_client_connect_lock:
            server = channel.server
            if server.id in self.voice_clients:
                return self.voice_clients[server.id]

            payload = {
                'op': 4,
                'd': {
                    'guild_id': channel.server.id,
                    'channel_id': channel.id,
                    'self_mute': False,
                    'self_deaf': False
                }
            }

            await self.ws.send(utils.to_json(payload))
            await asyncio.wait_for(self._session_id_found.wait(), timeout=5.0, loop=self.loop)
            await asyncio.wait_for(self._voice_data_found.wait(), timeout=5.0, loop=self.loop)

            session_id = self.session_id
            voice_data = self._voice_data_found.data

            self._session_id_found.clear()
            self._voice_data_found.clear()

            kwargs = {
                'user': self.user,
                'channel': channel,
                'data': voice_data,
                'loop': self.loop,
                'session_id': session_id,
                'main_ws': self.ws
            }

            voice_client = VoiceClient(**kwargs)
            self.voice_clients[server.id] = voice_client
            await voice_client.connect()
            return voice_client

    async def get_player(self, channel, create=False):
        server = channel.server

        if server.id not in self.players:
            if not create:
                raise CommandError(
                    'Player does not exist. It has not been summoned yet into a voice channel.  '
                    'Use %ssummon to summon it to your voice channel.' % self.config.command_prefix)

            voice_client = await self.get_voice_client(channel)

            playlist = Playlist(self.loop)
            player = MusicPlayer(self, voice_client, playlist) \
                .on('play', self.on_play) \
                .on('resume', self.on_resume) \
                .on('pause', self.on_pause) \
                .on('stop', self.on_stop)

            player.skip_state = SkipState()
            self.players[server.id] = player

        return self.players[server.id]

    def on_play(self, player, entry):
        self.update_now_playing(entry)
        player.skip_state.reset()

        if self.config.now_playing_mentions:
            self.loop.create_task(self.send_message(entry.meta['channel'], '%s - your song **%s** is now playing in %s!' % (
                entry.meta['author'].mention, entry.title, player.voice_client.channel.name
            )))
        else:
            self.loop.create_task(self.send_message(entry.meta['channel'], 'Now playing in %s: **%s**' % (
                player.voice_client.channel.name, entry.title
            )))

    def on_resume(self, entry, **_):
        self.update_now_playing(entry)

    def on_pause(self, entry, **_):
        self.update_now_playing(entry, True)

    def on_stop(self, **_):
        self.update_now_playing()

    def update_now_playing(self, entry=None, is_paused=False):
        game = None
        if entry:
            prefix = u'\u275A\u275A ' if is_paused else ''

            name = u'{}{}'.format(prefix, entry.title)[:128]
            game = discord.Game(name=name)

        self.loop.create_task(self.change_status(game))

    # noinspection PyMethodOverriding
    def run(self):
        return super().run(self.config.username, self.config.password)

    async def on_ready(self):
        win_unicode_console.enable()

        print('Connected!\n')
        print('Username: %s' % self.user.name)
        print('Bot ID: %s' % self.user.id)
        print('Owner ID: %s' % self.config.owner_id)
        print()

        print("Command prefix is %s" % self.config.command_prefix)
        # print("Days active required to use commands is %s" % self.config.days_active) # NYI
        print("Skip threshold at %s votes or %g%%" % (self.config.skips_required, self.config.skip_ratio_required*100))
        print("Whitelist check is %s" % ['disabled', 'enabled'][self.config.white_list_check])
        print("Now Playing message @mentions are %s" % ['disabled', 'enabled'][self.config.now_playing_mentions])
        print("Autosummon is %s" % ['disabled', 'enabled'][self.config.auto_summon])
        print()

        if self.servers:
            print('--Server List--')
            [print(s) for s in self.servers]
        else:
            print("No servers have been joined yet.")

        print()

        if self.config.owner_id == self.user.id:
            print(
                "[Notice] You have either set the OwnerID config option to the bot's id instead "
                "of yours, or you've used your own credentials to log the bot in instead of the "
                "bot's account (the bot needs its own account to work properly).")

        # maybe option to leave the ownerid blank and generate a random command for the owner to use

        if self.config.auto_summon:
            await self._auto_summon()


    async def _auto_summon(self):
        for server in self.servers:
            for channel in server.channels:
                if discord.utils.get(channel.voice_members, id=self.config.owner_id):
                    print("Owner found in %s/%s" % (server, channel))
                    await self.handle_summon(channel, discord.Object(id=str(self.config.owner_id)))
                    return

        print("Owner not found in a voice channel, could not autosummon.")


    async def handle_help(self, message):
        """
        Usage: {command_prefix}help
        Prints a help message
        """
        helpmsg = "https://github.com/SexualRhinoceros/MusicBot/wiki/Commands-list" # THIS IS TEMPORARY
        # Maybe there's a clever way to do this
        return Response(helpmsg, reply=True, delete_after=60)

    async def handle_whitelist(self, message, option, username):
        """
        Usage: {command_prefix}whitelist [ + | - | add | remove ] @UserName
        Adds or removes the user to the whitelist. When the whitelist is enabled,
        whitelisted users are permitted to use bot commands.
        """
        if message.author.id != self.config.owner_id:
            return

        user_id = extract_user_id(username)
        if not user_id:
            raise CommandError('Invalid user specified')

        if option not in ['+', '-', 'add', 'remove']:
            raise CommandError('Invalid option "%s" specified, use +, -, add, or remove' % option)

        if option in ['+', 'add']:
            self.whitelist.add(user_id)
            write_file('./config/whitelist.txt', self.whitelist)

            return Response('user has been added to the whitelist', reply=True, delete_after=10)

        else:
            if user_id not in self.whitelist:
                return Response('user is not in the whitelist', reply=True, delete_after=10)

            else:
                self.whitelist.remove(user_id)
                write_file('./config/whitelist.txt', self.whitelist)

                return Response('user has been removed from the whitelist', reply=True, delete_after=10)


    async def handle_blacklist(self, message, option, username):
        """
        Usage: {command_prefix}blacklist [ + | - | add | remove ] @UserName
        Adds or removes the user to the blacklist. Blacklisted users are forbidden from
        using bot commands. Blacklisting a user also removes them from the whitelist.
        """
        if message.author.id != self.config.owner_id:
            return

        user_id = extract_user_id(username)
        if not user_id:
            raise CommandError('Invalid user specified')

        if str(user_id) == self.config.owner_id:
            return Response("The owner cannot be blacklisted.", delete_after=10)

        if option not in ['+', '-', 'add', 'remove']:
            raise CommandError('Invalid option "%s" specified, use +, -, add, or remove' % option)

        if option in ['+', 'add']:
            self.blacklist.add(user_id)
            write_file('./config/blacklist.txt', self.blacklist)

            if user_id in self.whitelist:
                self.whitelist.remove(user_id)
                write_file('./config/whitelist.txt', self.whitelist)
                return Response('user has been added to the blacklist and removed from the whitelist', reply=True, delete_after=10)

            else:
                return Response('user has been added to the blacklist', reply=True, delete_after=10)

        else:
            if user_id not in self.blacklist:
                return Response('user is not in the blacklist', reply=True, delete_after=10)

            else:
                self.blacklist.remove(user_id)
                write_file('./config/blacklist.txt', self.blacklist)

                return Response('user has been removed from the blacklist', reply=True, delete_after=10)


    async def handle_id(self, author):
        """
        Usage: {command_prefix}id
        Tells the user their id.
        """
        return Response('your id is `%s`' % author.id, reply=True)

    async def handle_joinserver(self, message, server_link):
        """
        Usage {command_prefix}joinserver [Server Link]
        Asks the bot to join a server. [todo: add info about if it breaks or whatever]
        """
        try:
            if message.author.id == self.config.owner_id:
                await self.accept_invite(server_link)

        except:
            raise CommandError('Invalid URL provided:\n{}\n'.format(server_link))

    async def handle_play(self, player, channel, author, song_url):
        """
        Usage {command_prefix}play [song link]
        Adds the song to the playlist.
        """

        try:
            await self.send_typing(channel)

            reply_text = "Enqueued **%s** to be played. Position in queue: %s"

            if 'playlist?list' in song_url:
                print('Playlist song url:', song_url)

                t0 = time.time()

                # My test was 1.2 seconds per song, but we maybe should fudge it a bit, unless we can
                # monitor it and edit the message with the estimated time, but that's some ADVANCED SHIT
                # I don't think we can hook into it anyways, so this will have to do.
                # It would probably be a thread to check a few playlists and get the speed from that
                # Different playlists might download at different speeds though
                wait_per_song = 1.2

                info = await extract_info(player.playlist.loop, song_url, download=False, process=False)
                num_songs = sum(1 for _ in info['entries'])

                # This message can be deleted after playlist processing is done.
                procmesg = await self.send_message(channel,
                    'Gathering playlist information for {} songs{}'.format(
                        num_songs,
                        ', ETA: {:g} seconds'.format(num_songs*wait_per_song) if num_songs >= 10 else '.'))

                # We don't have a pretty way of doing this yet.  We need either a loop
                # that sends these every 10 seconds or a nice context manager.
                await self.send_typing(channel)

                entry_list, position = await player.playlist.import_from(song_url, channel=channel, author=author)
                entry = entry_list[0]

                tnow = time.time()
                ttime = tnow - t0

                print("Processed {} songs in {:.2g} seconds at {:.2f}s/song, {:+.2g}/song from expected".format(
                    len(entry_list), ttime, ttime/len(entry_list), ttime/len(entry_list) - wait_per_song))

                await self.delete_message(procmesg)

            else:
                entry, position = await player.playlist.add_entry(song_url, channel=channel, author=author)

            time_until = await player.playlist.estimate_time_until(position, player)

            if position == 1 and player.is_stopped:
                position = 'Up next!'
                reply_text = reply_text % (entry.title, position)
            else:
                reply_text += ' - estimated time until playing: %s'
                reply_text = reply_text % (entry.title, position, time_until)
                # TODO: Subtract time the current song has been playing for

            return Response(reply_text, reply=True, delete_after=15)

        except Exception as e:
            traceback.print_exc()
            raise CommandError('Unable to queue up song at %s to be played.' % song_url)

    async def handle_summon(self, channel, author):
        """
        Usage {command_prefix}summon
        This command is for summoning the bot into your voice channel [but it should do it automatically the first time]
        """
        if self.voice_clients:
            raise CommandError("Multiple servers not supported at this time.")

        # moving = False
        # if channel.server.id in self.players:
        #     moving = True
        #     print("Already in channel, moving")


        server = channel.server

        channel = None
        for channel in server.channels:
            if discord.utils.get(channel.voice_members, id=author.id):
                break

        if not channel:
            raise CommandError('You are not in a voice channel!')

        # if moving:
        #     await self.move_member(channel.server.me, channel)
        #     return Response('ok?')

        player = await self.get_player(channel, create=True)

        if player.is_stopped:
            player.play()

    async def handle_pause(self, player):
        """
        Usage {command_prefix}pause
        Pauses playback of the current song. [todo: should make sure it works fine when used inbetween songs]
        """

        if player.is_playing:
            player.pause()

        else:
            raise CommandError('Player is not playing.')

    async def handle_resume(self, player):
        """
        Usage {command_prefix}resume
        Resumes playback of a paused song.
        """
        if player.is_paused:
            player.resume()

        else:
            raise CommandError('Player is not paused.')

    async def handle_shuffle(self, player):
        """
        Usage {command_prefix}shuffle
        Shuffles the playlist.
        """
        player.playlist.shuffle()
        return Response('*shuffleshuffleshuffle*', delete_after=10)

    async def handle_skip(self, player, channel, author):
        """
        Usage {command_prefix}skip
        Skips the current song when enough votes are cast, or by the bot owner.
        """

        if player.is_stopped or player.is_paused: # TODO: pausing and skipping a song breaks /something/, i'm not sure what
            raise CommandError("Can't skip! The player is not playing!")

        if author.id == self.config.owner_id:
            player.skip()
            return

        voice_channel = player.voice_client.channel

        num_voice = sum(1 for m in voice_channel.voice_members if not (
            m.deaf or m.self_deaf or m.id == str(self.config.owner_id)))

        num_skips = player.skip_state.add_skipper(author.id)

        skips_remaining = min(self.config.skips_required, round(num_voice * self.config.skip_ratio_required)) - num_skips

        if skips_remaining <= 0:
            player.skip()
            return Response(
                'your skip for **{}** was acknowledged.'
                '\nThe vote to skip has been passed.{}'.format(
                    player.current_entry.title,
                    ' Next song coming up!' if player.playlist.peek() else ''
                ),
                reply=True,
                delete_after=10
            )

        else:
            # TODO: When a song gets skipped, delete the old x needed to skip messages
            return Response(
                'your skip for **{}** was acknowledged.'
                '\n**{}** more {} required to vote to skip this song.'.format(
                    player.current_entry.title,
                    skips_remaining,
                    'person is' if skips_remaining == 1 else 'people are'
                ),
                reply=True
            )

    async def handle_volume(self, message, new_volume=None):
        """
        Usage {command_prefix}volume (+/-)[volume]
        Sets the playback volume. Accepted values are from 1 to 100.
        Putting + or - before the volume will make the volume change relative to the current volume.
        """

        player = await self.get_player(message.channel)

        if not new_volume:
            return Response('Current volume: `%s%%`' % int(player.volume * 100), reply=True, delete_after=10)

        relative = False
        if new_volume[0] in '+-':
            relative = True

        try:
            new_volume = int(new_volume)

        except ValueError:
            raise CommandError('{} is not a valid number'.format(new_volume))

        if relative:
            vol_change = new_volume
            new_volume += (player.volume * 100)

        old_volume = int(player.volume * 100)

        if 0 < new_volume <= 100:
            player.volume = new_volume / 100.0

            return Response('updated volume from %d to %d' % (old_volume, new_volume), reply=True, delete_after=10)

        else:
            if relative:
                raise CommandError(
                    'Unreasonable volume change provided: {}{:+} -> {}%.  Provide a change between {} and {:+}.'.format(
                        old_volume, vol_change, old_volume + vol_change, 1 - old_volume, 100 - old_volume))
            else:
                raise CommandError(
                    'Unreasonable volume provided: {}%. Provide a value between 1 and 100.'.format(new_volume))

    async def handle_8(self, channel):
        """
        Usage {command_prefix}8
        returns 8ball answer.
        """
        ball = ["As I see it, yes", "It is certain", "It is decidedly so", "Most likely", "Outlook good", "Signs point to yes", "Without a doubt", "Yes", "Yes – definitely", "You may rely on it", "Reply hazy, try again", "Ask again later", "Better not tell you now", "Cannot predict now", "Concentrate and ask again", "Don't count on it", "My reply is no", "My sources say no", "Outlook not so good", "Very doubtful"]
        
        message = choice(ball)
        return Response(message, reply=True)
        
    async def handle_twitchalert(self, message, username):
        global twitchStreams
        twitchStreams = dataIO.fileIO("json/twitch.json", "load")
        added = False
        user_id = extract_user_id(username)
        if True:
            msg = message.content.split(" ")
            if len(msg) == 2:
                for i, stream in enumerate(twitchStreams):
                    if stream["NAME"] == msg[1] and message.channel.id in stream["CHANNELS"]:
                        fmsg = "`I'm already monitoring that stream in this channel.`"
                        return False
                for stream in twitchStreams:
                    if stream["NAME"] == msg[1] and message.channel.id not in stream["CHANNELS"]: # twitchAlert is already monitoring this streamer but not in this channel
                        twitchStreams[i]["CHANNELS"].append(message.channel.id)
                        added = True
                if not added: # twitchAlert wasn't monitoring this streamer
                    twitchStreams.append({"CHANNELS" : [message.channel.id], "NAME" : msg[1], "ALREADY_ONLINE" : False})

                dataIO.fileIO("json/twitch.json", "save", twitchStreams)
                fmsg = "`I will always send an alert in this channel whenever {}'s stream is online. Use !stoptwitchalert [name] to stop it.`".format(msg[1])
            else:
               fmsg = "`!twitchalert [name]`"
        else:
            fmsg = "`I don't take orders from you.`"
        return Response(fmsg, reply=True)

    async def hande_stoptwitchalert(self, message):
        global twitchStreams
        twitchStreams = dataIO.fileIO("json/twitch.json", "load")
        if isMemberAdmin(message):
            msg = message.content.split(" ")
            if len(msg) == 2:
                for i, stream in enumerate(twitchStreams):
                    if stream["NAME"] == msg[1] and message.channel.id in stream["CHANNELS"]:
                        if len(stream["CHANNELS"]) == 1:
                            twitchStreams.remove(stream)
                        else:
                            twitchStreams[i]["CHANNELS"].remove(message.channel.id)
                        dataIO.fileIO("json/twitch.json", "save", twitchStreams)
                        fmsg = "`I will stop sending alerts about {}'s stream in this channel.`".format(msg[1])
                        return True
                fmsg = "`There's no alert for {}'s stream in this channel.`".format(msg[1])
            else:
                fmsg = "`!stoptwitchalert [name]`"
        else:
           fsmg = "`I don't take orders from you.`"
        return Response(fmsg, reply=True)
        
    async def handle_twitch(self, message, channel):
        """
        Usage {command_prefix}twitch
        checks if twitch stream is online
        """
        msg = message.content.split()
        if len(msg) == 2:
            try:
                url =  "https://api.twitch.tv/kraken/streams/" + msg[1]
                async with aiohttp.get(url) as r:
                    data = await r.json()
                if "error" in data:
                    fmsg = "`There is no streamer named {}`".format(msg[1])
                elif "stream" in data:
                    if data["stream"] != None:
                        fmsg = "`{} is online!` {}".format(msg[1], "http://www.twitch.tv/" + msg[1])
                    else:
                        fmsg = "`{} is offline.`".format(msg[1])
                else:
                    fmsg = "`There is no streamer named {}`".format(msg[1])
            except:
                fmsg = "{} `Error.`"
        else:
            fsmg = "{} `!twitch [stream]`"
        return Response(fmsg, reply=True)
        

    async def handle_queue(self, channel):
        """
        Usage {command_prefix}queue
        Prints the current song queue.
        """
        player = await self.get_player(channel)

        lines = []
        unlisted = 0

        # TODO: Add "Now Playing: ..."

        for i, item in enumerate(player.playlist, 1):
            nextline = '{}) **{}** added by **{}**'.format(i, item.title, item.meta['author'].name).strip()
            currentlinesum = sum([len(x)+1 for x in lines]) # +1 is for newline char

            # This is fine I guess, don't need to worry too much about trying to squeeze as much in as possible
            if currentlinesum + len(nextline) + len('* ... and xxx more*') > DISCORD_MSG_CHAR_LIMIT:
                if currentlinesum + len('* ... and xxx more*'):
                    unlisted += 1
                    continue

            lines.append(nextline)

        if unlisted:
            lines.append('\n*... and %s more*' % unlisted)

        if not lines:
            lines.append(
                'There are no songs queued! Queue something with {}play.'.format(self.config.command_prefix))

        message = '\n'.join(lines)
        return Response(message, delete_after=30)


    async def handle_clean(self, message, author, amount):
        """
        Usage {command_prefix}clean amount
        Removes [amount] messages the bot has posted in chat.
        """
        pass



    async def on_message(self, message):
        if message.author == self.user:
            if message.content.startswith(self.config.command_prefix):
                print("Ignoring command from myself (%s)" % message.content)
            return

        if message.channel.is_private:
            await self.send_message(message.channel, 'You cannot use this bot in private messages.')
            return

        message_content = message.content.strip()
        if not message_content.startswith(self.config.command_prefix):
            return

        command, *args = message_content.split()
        command = command[len(self.config.command_prefix):].lower().strip()

        handler = getattr(self, 'handle_%s' % command, None)
        if not handler:
            return


        if int(message.author.id) in self.blacklist and message.author.id != self.config.owner_id:
            print("[Blacklisted] {0.id}/{0.name} ({1})".format(message.author, message_content))
            return

        elif self.config.white_list_check and int(message.author.id) not in self.whitelist and message.author.id != self.config.owner_id:
            print("[Not whitelisted] {0.id}/{0.name} ({1})".format(message.author, message_content))
            return

        else:
            print("[Command] {0.id}/{0.name} ({1})".format(message.author, message_content))


        argspec = inspect.signature(handler)
        params = argspec.parameters.copy()

        # noinspection PyBroadException
        try:
            handler_kwargs = {}
            if params.pop('message', None):
                handler_kwargs['message'] = message

            if params.pop('channel', None):
                handler_kwargs['channel'] = message.channel

            if params.pop('author', None):
                handler_kwargs['author'] = message.author

            if params.pop('player', None):
                handler_kwargs['player'] = await self.get_player(message.channel)

            args_expected = []
            for key, param in list(params.items()):
                doc_key = '[%s=%s]' % (key, param.default) if param.default is not inspect.Parameter.empty else key
                args_expected.append(doc_key)

                if not args and param.default is not inspect.Parameter.empty:
                    params.pop(key)
                    continue

                if args:
                    arg_value = args.pop(0)
                    handler_kwargs[key] = arg_value
                    params.pop(key)

            if params:
                docs = getattr(handler, '__doc__', None)
                if not docs:
                    docs = 'Usage: {}{} {}'.format(
                        self.config.command_prefix,
                        command,
                        ' '.join(args_expected)
                    )

                docs = '\n'.join(l.strip() for l in docs.split('\n'))
                await self.send_message(
                    message.channel,
                    '```\n%s\n```' % docs.format(command_prefix=self.config.command_prefix)
                )
                return

            response = await handler(**handler_kwargs)
            if response and isinstance(response, Response):
                content = response.content
                if response.reply:
                    content = '%s, %s' % (message.author.mention, content)

                sentmsg = await self.send_message(message.channel, content)

                if response.delete_after > 0:
                    await asyncio.sleep(response.delete_after)
                    await self.delete_message(sentmsg)
                    # TODO: Add options for deletion toggling

        except CommandError as e:
            await self.send_message(message.channel, '```\n%s\n```' % e.message)

        except:
            await self.send_message(message.channel, '```\n%s\n```' % traceback.format_exc())
            traceback.print_exc()

    @asyncio.coroutine
    async def twitchAlert():
        global twitchStreams
        twitchStreams = dataIO.fileIO("json/twitch.json", "load")
        CHECK_DELAY = 10
        while True:
            if twitchStreams and client.is_logged_in:
                to_delete = []
                save = False
                consistency_check = twitchStreams
                for i, stream in enumerate(twitchStreams):
                    if twitchStreams == consistency_check: #prevents buggy behavior if twitchStreams gets modified during the iteration
                        try:
                            url =  "https://api.twitch.tv/kraken/streams/" + stream["NAME"]
                            async with aiohttp.get(url) as r:
                                data = await r.json()
                            if "status" in data: 
                                if data["status"] == 404: #Stream doesn't exist, remove from list
                                    to_delete.append(stream)
                            elif "stream" in data:
                                if data["stream"] != None:
                                    if not stream["ALREADY_ONLINE"]:
                                        for channel in stream["CHANNELS"]:
                                            try:
                                                await client.send_message(client.get_channel(channel), "`{} is online!` {}".format(stream["NAME"], "http://www.twitch.tv/" + stream["NAME"]))
                                            except: #In case of missing permissions
                                                pass
                                        twitchStreams[i]["ALREADY_ONLINE"] = True
                                        save = True
                                else:
                                    if stream["ALREADY_ONLINE"]:
                                        twitchStreams[i]["ALREADY_ONLINE"] = False
                                        save = True
                        except Exception as e:
                            logger.warning(e)

                        if save: #Saves online status, in case the bot needs to be restarted it can prevent message spam
                            dataIO.fileIO("json/twitch.json", "save", twitchStreams)
                            save = False

                        await asyncio.sleep(CHECK_DELAY)
                    else:
                        break

                if to_delete:
                    for invalid_stream in to_delete:
                        twitchStreams.remove(invalid_stream)
                    dataIO.fileIO("json/twitch.json", "save", twitchStreams)
            else:
                await asyncio.sleep(5)

				
def loadDataFromFiles(loadsettings=False):
	global twitchStreams
	twitchStreams = dataIO.fileIO("json/twitch.json", "load")
	logger.info("Loaded " + str(len(twitchStreams)) + " streams to monitor.")

	if loadsettings:
		global settings
		settings = dataIO.fileIO("json/settings.json", "load")
		
if __name__ == '__main__':
    global logger
    logger = loggerSetup()
    dataIO.logger = logger

    dataIO.migration()
    dataIO.createEmptyFiles()

    settings = dataIO.loadAndCheckSettings()

    loadDataFromFiles()
    bot = MusicBot()
    bot.run()
    loop.create_task(twitchAlert())


'''
TODOs:
  Deleting messages
    Maybe Response objects can have a parameter that deletes the message
    Probably should have an section for it in the options file
    If not, we should have a cleanup command, or maybe have one anyways

  Command to clear the queue, either a `!skip all` argument or a `!clear` or `!queue clear` or whatever

'''
