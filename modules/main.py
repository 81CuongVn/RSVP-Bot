import asyncio
import logging
import re
import typing

import pendulum
import discord
from discord.ext import commands, tasks
from tinymongo import TinyMongoClient

import constants
import exceptions
from modules import utility

mclient = TinyMongoClient('tinydb')

class Background(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._rsvp_triggers.start() #pylint: disable=no-member

    def cog_unload(self):
        self._rsvp_triggers.stop() #pylint: disable=no-member

    @tasks.loop(minutes=1)
    async def _rsvp_triggers(self):
        print('start task')
        reservations = mclient.rsvpbot.reservations.find({'active': True})
        for rsvp in reservations:
            config = mclient.rsvpbot.config.find_one({'_id': rsvp['guild']})
            start_date = pendulum.from_timestamp(rsvp['date'], tz=utility.timezone_alias(rsvp['timezone']))
            current_date = pendulum.now(utility.timezone_alias(rsvp['timezone']))

            date_diff = start_date - current_date
            human_diff = current_date.add(seconds=date_diff.seconds).diff_for_humans()
            if date_diff.seconds <= 7200 and not rsvp['admin_reminder']: # 2 hours prior, and first notification
                participant_count = len(rsvp["participants"])
                tanks = 0
                healers = 0
                dps = 0

                for user in rsvp['participants']:
                    if user['role'] == 'tank':
                        tanks += 1

                    elif user['role'] == 'healer':
                        healers += 1
                    
                    elif user['role'] == 'dps':
                        dps += 1

                if tanks < constants.TANK_COUNT or healers < constants.HEALER_COUNT or dps < constants.DPS_COUNT or participant_count < constants.TOTAL_COUNT:
                    alert_roles = []
                    for x in config['access_roles']:
                        alert_roles.append(f'<@&{x}>')

                    role_mentions = ' '.join(alert_roles)
                    admin_channel = self.bot.get_channel(config['admin_channel'])


                    try:
                        await admin_channel.send(f'{role_mentions} Raid event notification: scheduled raid {human_diff} has less members than minimum threshold for an event.\n' \
                                                 f':man_raising_hand: **{participant_count}** user{utility.plural(participant_count)} {"is" if participant_count == 1 else "are"} signed up. Of these there are ' \
                                                 f'**{tanks}** {constants.EMOJI_TANK}tank{utility.plural(tanks)}, **{healers}** {constants.EMOJI_HEALER}healer{utility.plural(healers)}, and **{dps}** {constants.EMOJI_DPS}dps.')

                    except discord.Forbidden:
                        if admin_channel:
                            logging.error(f'[RSVP Bot] Unable to send low player count alert to admins. Guild ({admin_channel.guild}) | Channel ({admin_channel.channel}), aborted')

                    mclient.rsvpbot.reservations.update_one({'_id': rsvp['_id']}, {'$set': {
                        'admin_reminder': True
                    }})

            if date_diff.seconds <= 900 and not rsvp['user_reminder']: # 15 minutes prior, and first notification
                pass # TODO

class Main(commands.Cog, name='RSVP Bot'):
    def __init__(self, bot):
        self.bot = bot
        self.READY = False
        self.REACT_EMOJI = [
            constants.EMOJI_TANK,
            constants.EMOJI_HEALER,
            constants.EMOJI_DPS,
            constants.EMOJI_TENTATIVE,
            constants.EMOJI_LATE,
            constants.EMOJI_CANCEL
        ]
        self.EMOJI_MAPPING = {
            constants.EMOJI_DPS: 'dps',
            constants.EMOJI_TANK: 'tank',
            constants.EMOJI_HEALER: 'healer',
            constants.EMOJI_TENTATIVE: 'tentative',
            constants.EMOJI_LATE: 'late',
            constants.EMOJI_CANCEL: 'cancel',
            constants.EMOJI_LEADER: 'host',
            constants.EMOJI_CONFIRMED: 'confirmed'
        }

    async def msg_wait(self, ctx, values: list, _int=False, _list=False, content=None, embed=None, timeout=60.0):
        def check(m):
            return m.author.id == ctx.author.id and m.channel.id == ctx.channel.id and m.content # Is same author, channel, and has content

        if content: content += ' Reply `cancel` to cancel this action'
        channelMsg = await ctx.send(content, embed=embed)

        while True:
            try:
                try:
                    message = await self.bot.wait_for('message', timeout=timeout, check=check)

                except asyncio.TimeoutError:
                    await channelMsg.edit(content=f'{ctx.author.mention} The action timed out because of inactivity. Run the command again to try again')
                    raise exceptions.UserCanceled

                if message.content.strip() == 'cancel':
                    await ctx.send('Action canceled. Run the command again to try again')
                    raise exceptions.UserCanceled

                if _int:
                    msg_content = message.content.strip()
                    if _list:
                        item_list = [int(x.strip()) for x in msg_content.split(',')]
                        for item in item_list:
                            if item not in values: raise exceptions.BadArgument

                            return item_list

                    else:
                        result = int(msg_content)
                        if result not in values:
                            raise exceptions.BadArgument

                        return result

                else:
                    msg_content = message.content.strip().lower()
                    if _list:
                        item_list = [x.strip() for x in msg_content.split(',')]
                        for item in item_list:
                            if item not in values: raise exceptions.BadArgument

                        return item_list

                    else:
                        if msg_content not in values:
                            raise exceptions.BadArgument

                        return msg_content

            except (exceptions.BadArgument, ValueError):
                if content:
                    channelMsg = await ctx.send('That value doesn\'t look right, please try again. ' + content, embed=embed)

                else:
                    channelMsg = await ctx.send('That value doesn\'t look right, please try again.', embed=embed)

    async def _allowed(ctx):
        guild = mclient.rsvpbot.config.find_one({'_id': ctx.guild.id})

        if not guild:
            print('guild not setup')
            # Guild not setup, command not allowed
            return False

        for role in ctx.author.roles:
            print(role)
            if role.id in guild['access_roles']:
                return True

        print('didnt find admin role')
        return False

    async def _rsvp_embed(self, bot, guild, rsvp=0, *, data=None):
        embed = discord.Embed(title='Raid Signup', color=0x3B6F4D)
        embed.set_footer(text='RSVP Bot © MattBSG 2020')

        if not isinstance(guild, discord.Guild): guild = bot.get_guild(guild)
        guild_doc = mclient.rsvpbot.config.find_one({'_id': guild.id})

        if rsvp:
            doc = mclient.rsvpbot.reservations.find_one({'_id': rsvp})
            if not doc:
                raise exceptions.NotFound('Reservation does not exist')

            leader = guild.get_member(doc['host'])
            if not leader: # Left, or uncached. Pull from api
                leader = await bot.fetch_user(doc['host'])

            user_aliases = {}
            user_db = mclient.rsvpbot.users
            alias_docs = user_db.find({'_id': {'$in': [x['user'] for x in doc['participants']]}})
            for x in alias_docs:
                user_aliases[x['_id']] = x['alias']

            participants = []
            for x in doc['participants']:
                user = guild.get_member(x['user'])
                if not user: # Left, or uncached. Pull from api
                    user = await bot.fetch_user(x['user'])

                participants.append({
                    'user': user,
                    'alias': None if not user.id in user_aliases else user_aliases[user.id],
                    'role': x['role'],
                    'status': x['status']
                })

            data = {
                'date': pendulum.from_timestamp(doc['date'], tz=utility.timezone_alias(doc['timezone'])),
                'timezone': doc['timezone'],
                'description': doc['description'],
                'host': leader,
                'participants': participants
            }

        embed.description = f'{data["description"]}\n\n:man_raising_hand: {len(data["participants"])} Players signed up\n' \
                            f':alarm_clock: Scheduled to start **{data["date"].format("MMM Do, Y at h:mmA")} {data["timezone"].capitalize()}**'
        tanks = []
        healers = []
        dps = []
        for player in data['participants']:
            print(player)
            user = player
            status = user['status']
            if user['alias']:
                user_alias = user['alias']

            elif isinstance(user['user'], discord.Member):
                user_alias = user['user'].name if not user['user'].nick else user['user'].nick

            else:
                user_alias = user['user'].name

            if status == 'confirmed' and data['host'].id == user['user'].id:
                status = 'host'

            if user['role'] == 'tank':
                tanks.append(constants.STATUS_MAPPING[status] + user_alias)

            elif user['role'] == 'healer':
                healers.append(constants.STATUS_MAPPING[status] + user_alias)

            else:
                dps.append(constants.STATUS_MAPPING[status] + user_alias)

        embed.add_field(name='Tanks', value='*No one yet*' if not tanks else '\n'.join(tanks), inline=True)
        embed.add_field(name='Healers', value='*No one yet*' if not healers else '\n'.join(healers), inline=True)
        embed.add_field(name='DPS', value='*No one yet*' if not dps else '\n'.join(dps), inline=True)
        embed.add_field(name='How to signup', value=f'To RSVP for this event please react below with the role you will ' \
        f'be playing; {constants.EMOJI_TANK}Tank, {constants.EMOJI_HEALER}Healer, or {constants.EMOJI_DPS}DPS.\n' \
        f'If you are not sure if you can make the event, react with your role as well as {constants.EMOJI_TENTATIVE}tentative. ' \
        f'Excepting to be __late__ for the event? React with your role as well as {constants.EMOJI_LATE}late.\n\n' \
        f'You may react {constants.EMOJI_CANCEL} to cancel your RSVP at any time. Should you want to unmark yourself as tentative ' \
        f'or late simply react again. More information found in <#{guild_doc["info_channel"]}>'
        )

        rsvp_channel = bot.get_channel(guild_doc['rsvp_channel'])
        if rsvp:
            message = await rsvp_channel.fetch_message(rsvp) # TODO: check if message exists first
            await message.edit(embed=embed)

        else:
            message = await rsvp_channel.send(embed=embed)

        return message

    async def _create_reservation(self, bot, ctx, day, time, tz, desc):
        if tz.lower() in constants.TIMEZONE_ALIASES:
            timezone = pendulum.timezone(constants.TIMEZONE_ALIASES[tz.lower()])

        else:
            try:
                timezone = pendulum.timezone(tz.lower())

            except pendulum.tz.zoneinfo.exceptions.InvalidTimezone:
                raise exceptions.InvalidTz

        current_time = pendulum.now(timezone)

        try:
            event_time = pendulum.parse(time, tz=timezone, strict=False).on(current_time.year, current_time.month, current_time.day)

        except pendulum.parsing.exceptions.ParserError:
            raise exceptions.InvalidTime

        if day.lower() not in constants.DAY_MAPPING:
            raise exceptions.InvalidDOW

        if current_time.day_of_week == constants.DAY_MAPPING[day.lower()] and event_time > current_time:
            # Same as today, but in future
            event_start = event_time

        else:
            # In the future or current day (but already elasped)
            event_start = event_time.next(constants.DAYS[constants.DAY_MAPPING[day.lower()]]).at(event_time.hour, event_time.minute)

        rsvp_event = {
            'host': ctx.author,
            'channel': ctx.channel.id,
            'guild': ctx.guild.id,
            'date': event_start,
            'timezone': tz.lower(),
            'description': desc,
            'created_at': pendulum.now('UTC').int_timestamp,
            'participants': [],
            'admin_reminder': False,
            'user_reminder': False,
            'active': True
        }
        rsvp_message = await self._rsvp_embed(bot, ctx.guild, data=rsvp_event)
        rsvp_event['_id'] = rsvp_message.id
        rsvp_event['host'] = ctx.author.id
        rsvp_event['date'] = event_start.int_timestamp

        mclient.rsvpbot.reservations.insert_one(rsvp_event)

        return event_start.format('MMM Do, Y at h:mmA') + ' ' + tz.lower().capitalize(), rsvp_message

    @commands.command(name='setup')
    async def _setup(self, ctx):
        """
        Perform server setup with RSVP Bot.

        Can be used to setup the bot for the first time, or to change settings.
        Example usage:
            setup
        """
        db = mclient.rsvpbot.config

        app_info = await self.bot.application_info()
        if ctx.author.id not in [app_info.owner.id, ctx.guild.owner.id]:
            return await ctx.send(f'{ctx.author.mention} You must be the owner of this server or bot to use this command')

        setup = db.find_one({'_id': ctx.guild.id})

        try:
            rsvp_channel = await self.msg_wait(ctx, [x.id for x in ctx.guild.channels], _int=True, content=f'Hi, I\'m RSVP Bot. Let\'s get your server setup to use raid rsvp features. First off, what channel would you like RSVP signups in? Please send the channel ID (i.e. {ctx.guild.channels[0].id}).')
            info_channel = await self.msg_wait(ctx, [x.id for x in ctx.guild.channels], _int=True, content=f'Thanks. Now, what channel can users find more information about raids? This can be anything such as a rules, info or faq channel). Please send the channel ID (i.e. {ctx.guild.channels[0].id}).')
            admin_channel = await self.msg_wait(ctx, [x.id for x in ctx.guild.channels], _int=True, content=f'Cool. Where should notifications to admins be sent? Please send the channel ID (i.e. {ctx.guild.channels[0].id}).')
            # Confirm role is handy and get role(s)
            await self.msg_wait(ctx, ['confirm'], content=f'Lets get some roles that will have admin priviledges; you can specify just one or as many as you would like. Once you have the roles created please gather their IDs.\n\n**Let me know once you have them by replying "confirm".**', timeout=180.0)
            rsvp_admins = await self.msg_wait(ctx, [x.id for x in ctx.guild.roles], _int=True, _list=True, content=f'Awesome. Please send the IDs of all roles that should have admin priviledges. This can be just one ID, or a comma seperated list (i.e. id1, id2, id3).', timeout=120.0)

            if not setup:
                mclient.rsvpbot.config.insert_one({
                    '_id': ctx.guild.id,
                    'rsvp_channel': rsvp_channel,
                    'info_channel': info_channel,
                    'admin_channel': admin_channel,
                    'access_roles': [rsvp_admins] if isinstance(rsvp_admins, int) else rsvp_admins,
                    'invite_message': 'Raid is about to begin. Please log in for an invite and summon.'
                })

            else:
                mclient.rsvpbot.config.update_one({'_id': ctx.guild.id}, {
                    '_id': ctx.guild.id,
                    'rsvp_channel': rsvp_channel,
                    'info_channel': info_channel,
                    'admin_channel': admin_channel,
                    'access_roles': [rsvp_admins] if isinstance(rsvp_admins, int) else rsvp_admins
                })

            return await ctx.send(f'All set! Your guild has been setup. Use the `{ctx.prefix}help` command for a list of commands')

        except exceptions.UserCanceled:
            return

        except discord.Forbidden:
            logging.error(f'[RSVP Bot] Unable to respond to setup command. Guild ({ctx.guild}) | Channel ({ctx.channel}), aborted')
            return

        except Exception as e:
            print(e)

    @commands.group(name='rsvp', invoke_without_command=True)
    @commands.check(_allowed)
    async def _rsvp(self, ctx, day, time, timezone, *, description):
        """
        Creates a new RSVP reservation.

        Timezone is an alias set in the config or a raw timezone name (http://www.timezoneconverter.com/cgi-bin/findzone.tzc)
        Example usage:
            rsvp friday 10pm eastern Join us for a casual late night raid
            rsvp tuesday 1:15am America/New_York Who said early morning was too early?
        """
        config = mclient.rsvpbot.config.find_one({'_id': ctx.guild.id})

        time_to, rsvp_message = await self._create_reservation(self.bot, ctx, day, time, timezone, description)

        await ctx.send(f'Success! Event created starting {time_to}')
        for emoji in self.REACT_EMOJI:
            await rsvp_message.add_reaction(emoji)

    @_rsvp.command(name='alias')
    @commands.check(_allowed)
    async def _rsvp_alias(self, ctx, mode, member: discord.Member, alias=None):
        """
        Creates a reservation alias for a user.

        This will change the display name of a user to the alias in rsvp embeds
        Example usage:
            rsvp alias set @MattBSG#8888 Matt
            rsvp alias clear @MattBSG#8888
        """
        mode = mode.lower()
        if mode not in ['set', 'clear']:
            # Invalid mode
            await ctx.send(f':x: {ctx.author.mention} Provided mode "{mode}" is not valid. Must be either "set" or "clear"')

        if mode == 'set': 
            if not alias: await ctx.send(f':x: {ctx.author.mention} A name to alias this user to is required')
            new_alias = alias if mode == 'set' else None
            user_db = mclient.rsvpbot.users
            if user_db.find_one({'_id': member.id}):
                user_db.update_one({'_id': member.id}, {
                    '$set': {
                        'alias': alias
                    }
                })

            else:
                user_db.insert_one({
                    '_id': member.id,
                    'alias': alias
                })

            await ctx.send(f':white_check_mark: {ctx.author.mention}  Success! Alias for {member} has been set to `{alias}`')

        else:
            mclient.rsvpbot.users.delete_one({'_id': member.id})
            await ctx.send(f':white_check_mark: {ctx.author.mention} Success! Alias for {member} has been cleared')


    @_rsvp.command(name='message', aliases=['msg'])
    @commands.check(_allowed)
    async def _rsvp_invite_msg(self, ctx, *, content):
        """
        Sets the invitation message used.

        The invitation message is used when alerting users a raid
        is about to begin. 
        Example:
            rsvp message The raid will be starting soon, please login and join the voice channel!
        """
        mclient.rsvpbot.config.update_one({'_id': ctx.guild.id}, {
            '$set': {
                'invite_message': content
            }
        })

        await ctx.send(f':white_check_mark: {ctx.author.mention}  Success! RSVP invite message set: ```\n{content}```')

    @_rsvp.command(name='cancel')
    @commands.check(_allowed)
    async def _rsvp_cancel(self, ctx, message: typing.Union[int, str]):
        """
        Cancels a reservation that is waiting for players.

        Will cancel a reservation when provided with it's message ID or link
        Example:
            rsvp cancel 748924482894430278
            rsvp cancel https://discordapp.com/channels/133055605479964672/133055605479964672/748924482894430278
        """
        if isinstance(message, int):
            messageID = message

        else: # String
            match = re.search(r'https:\/\/\w*\.?discord(?:app)?.com\/channels\/\d+\/\d+\/(\d+)', message, flags=re.I)
            if not match:
                return await ctx.send(f':x: {ctx.author.mention} The message provided is invalid. Make sure you use a message ID or message link')

            messageID = match.group(1)

        reservation = mclient.rsvpbot.reservations.find_one({'_id': messageID})
        if not reservation:
            return await ctx.send(f':x: {ctx.author.mention} That message is not an active RSVP')

        if not reservation['active']:
            return await ctx.send(f':x: {ctx.author.mention} That message is not an active RSVP')

        try:
            rsvp_message = await self.bot.get_channel(reservation['channel']).fetch_message(messageID)

        except (discord.NotFound, discord.Forbidden, AttributeError):
            return await ctx.send(f':x: {ctx.author.mention} That RSVP message either no longer exists or I unable to view it\'s channel')

        mclient.rsvpbot.reservations.update_one({'_id': reservation['_id']}, {
            '$set': {
                'active': False
            }
        })

        embed = rsvp_message.embeds[0]
        embed.color = 0xB84444
        embed.title = '[Canceled] ' + embed.title
        embed.remove_field(3)

        await rsvp_message.edit(embed=embed)
        await rsvp_message.clear_reactions()
        await ctx.send(f':white_check_mark: {ctx.author.mention} Success! That event has been canceled')

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        if payload.member.bot: return
        message = await self.bot.get_channel(payload.channel_id).fetch_message(payload.message_id)
        await message.remove_reaction(payload.emoji, payload.member)

        db = mclient.rsvpbot.reservations
        user_db = mclient.rsvpbot.users
        rsvp_msg = db.find_one({'_id': payload.message_id})
        if not rsvp_msg:
            return

        if payload.emoji.is_unicode_emoji():
            emoji = payload.emoji.name

        else:
            emoji = '<'
            if payload.emoji.animated: emoji += 'a'
            emoji += f':{payload.emoji.name}:{payload.emoji.id}>'

        if emoji not in self.REACT_EMOJI: return
        if emoji in [constants.EMOJI_DPS, constants.EMOJI_HEALER, constants.EMOJI_TANK]:
            print(rsvp_msg)
            for participant in rsvp_msg['participants']:
                if participant['user'] != payload.user_id: continue
                db.update_one({'_id': payload.message_id}, {
                    'participants': utility.field_pull(
                        db.find_one({'_id': payload.message_id})['participants'],
                        ['user', payload.user_id],
                        _dict=True
                    )
                })
                db.update_one({'_id': payload.message_id}, {
                    'participants': utility.field_push(
                        db.find_one({'_id': payload.message_id})['participants'],
                        {
                            'user': payload.user_id,
                            'alias': participant['alias'],
                            'role': self.EMOJI_MAPPING[emoji],
                            'status': participant['status']
                        }
                    )
                })

                return await self._rsvp_embed(self.bot, payload.guild_id, rsvp=payload.message_id)

            user_doc = user_db.find_one({'_id': payload.user_id})
            alias = None if not user_doc else user_doc['alias']
            db.update_one({'_id': payload.message_id}, {
                'participants': utility.field_push(
                    db.find_one({'_id': payload.message_id})['participants'],
                    {
                        'user': payload.user_id,
                        'alias': alias,
                        'role': self.EMOJI_MAPPING[emoji],
                        'status': 'confirmed'
                    }
                )
            })
            return await self._rsvp_embed(self.bot, payload.guild_id, rsvp=payload.message_id)

        elif emoji in [constants.EMOJI_LATE, constants.EMOJI_TENTATIVE]:
            for participant in rsvp_msg['participants']:
                if participant['user'] != payload.user_id: continue

                status = 'confirmed' if self.EMOJI_MAPPING[emoji] == participant['status'] else self.EMOJI_MAPPING[emoji]

                db.update_one({'_id': payload.message_id}, {
                    'participants': utility.field_pull(
                        db.find_one({'_id': payload.message_id})['participants'],
                        ['user', payload.user_id],
                        _dict=True
                    )
                })

                db.update_one({'_id': payload.message_id}, {
                    'participants': utility.field_push(
                        db.find_one({'_id': payload.message_id})['participants'],
                        {
                            'user': payload.user_id,
                            'alias': participant['alias'],
                            'role': participant['role'],
                            'status': status
                        }
                    )
                })

            return await self._rsvp_embed(self.bot, payload.guild_id, rsvp=payload.message_id)

        elif emoji == constants.EMOJI_CANCEL:
            if payload.user_id not in [x['user'] for x in rsvp_msg['participants']]: return
            db.update_one({'_id': payload.message_id}, {
                'participants': utility.field_pull(
                    db.find_one({'_id': payload.message_id})['participants'],
                    ['user', payload.user_id],
                    _dict=True
                )
            })

            return await self._rsvp_embed(self.bot, payload.guild_id, rsvp=payload.message_id)

def setup(bot):
    bot.add_cog(Main(bot))
    logging.info('[Extension] Main module loaded')
    #bot.add_cog(Background(bot))
    logging.info('[Extension] Background task module loaded')

def teardown(bot):
    bot.remove_cog('Main')
    logging.info('[Extension] Main module unloaded')
    #bot.remove_cog('Background')
    logging.info('[Extension] Background task module unloaded')
