import logging
from typing import Any, Callable

from tabulate import tabulate
from telegram import (Bot,
                      ParseMode,
                      ReplyKeyboardMarkup,
                      Update,
                      InlineKeyboardButton,
                      InlineKeyboardMarkup)
from telegram.error import NetworkError, TelegramError
from telegram.ext import CommandHandler, Updater, CallbackQueryHandler, MessageHandler, Filters

from enum import Enum

from freqtrade.rpc.__init__ import (rpc_status_table,
                                    rpc_trade_status,
                                    rpc_daily_profit,
                                    rpc_trade_statistics,
                                    rpc_balance,
                                    rpc_config,
                                    rpc_start,
                                    rpc_stop,
                                    rpc_forcesell,
                                    rpc_performance,
                                    rpc_count
                                    )

from freqtrade import __version__, exchange, OperationalException
from freqtrade.misc import get_list_type, ListType, update_config

# Remove noisy log messages
logging.getLogger('requests.packages.urllib3').setLevel(logging.INFO)
logging.getLogger('telegram').setLevel(logging.INFO)
logger = logging.getLogger(__name__)

_UPDATER: Updater = None
_CONF = {}
MESSAGE_HANDLER = range(1)
_UPDATED_COINS = []


class Conversation(Enum):
    IDLE = 0
    MAX_OPEN_TRADES = 1
    STAKE_AMOUNT = 2
    UPDATE_COINS = 3


_CONVERSATION = Conversation.IDLE


def init(config: dict) -> None:
    """
    Initializes this module with the given config,
    registers all known command handlers
    and starts polling for message updates
    :param config: config to use
    :return: None
    """
    global _UPDATER

    _CONF.update(config)
    if not is_enabled():
        return

    _UPDATER = Updater(token=config['telegram']['token'], workers=0)

    # Register command handler and start telegram message polling
    handles = [
        CommandHandler('status', _status),
        CommandHandler('profit', _profit),
        CommandHandler('balance', _balance),
        CommandHandler('start', _start),
        CommandHandler('stop', _stop),
        CommandHandler('forcesell', _forcesell),
        CommandHandler('performance', _performance),
        CommandHandler('daily', _daily),
        CommandHandler('count', _count),
        CommandHandler('config', _config),
        CommandHandler('help', _help),
        CommandHandler('version', _version)
    ]
    for handle in handles:
        _UPDATER.dispatcher.add_handler(handle)

    # Register Callback Query Handler for Inline keyboard markup
    _UPDATER.dispatcher.add_handler(CallbackQueryHandler(_callback))

    # Register message handler
    _UPDATER.dispatcher.add_handler(
        MessageHandler(Filters.text, _message_handler))

    _UPDATER.start_polling(
        clean=True,
        bootstrap_retries=-1,
        timeout=30,
        read_latency=60,
    )
    logger.info(
        'rpc.telegram is listening for following commands: %s',
        [h.command for h in handles]
    )


def cleanup() -> None:
    """
    Stops all running telegram threads.
    :return: None
    """
    if not is_enabled():
        return
    _UPDATER.stop()


def is_enabled() -> bool:
    """
    Returns True if the telegram module is activated, False otherwise
    """
    return bool(_CONF['telegram'].get('enabled', False))


def authorized_only(command_handler: Callable[[Bot, Update], None]) -> Callable[..., Any]:
    """
    Decorator to check if the message comes from the correct chat_id
    :param command_handler: Telegram CommandHandler
    :return: decorated function
    """
    def wrapper(*args, **kwargs):
        update = kwargs.get('update') or args[1]

        # Reject unauthorized messages
        chat_id = int(_CONF['telegram']['chat_id'])
        if int(update.message.chat_id) != chat_id:
            logger.info('Rejected unauthorized message from: %s',
                        update.message.chat_id)
            return wrapper

        logger.info('Executing handler: %s for chat_id: %s',
                    command_handler.__name__, chat_id)
        try:
            return command_handler(*args, **kwargs)
        except BaseException:
            logger.exception('Exception occurred within Telegram module')
    return wrapper


@authorized_only
def _status(bot: Bot, update: Update) -> None:
    """
    Handler for /status.
    Returns the current TradeThread status
    :param bot: telegram bot
    :param update: message update
    :return: None
    """

    # Check if additional parameters are passed
    params = update.message.text.replace('/status', '').split(' ') \
        if update.message.text else []
    if 'table' in params:
        _status_table(bot, update)
        return

    # Fetch open trade
    (error, trades) = rpc_trade_status()
    if error:
        send_msg(trades, bot=bot)
    else:
        for trademsg in trades:
            send_msg(trademsg, bot=bot)


@authorized_only
def _config(bot: Bot, update: Update) -> None:
    """
    Handler for /config
    """
    _send_inline_keyboard_markup(bot, [
        InlineKeyboardButton("View config", callback_data="view_config"),
        InlineKeyboardButton("Edit config", callback_data="edit_config"),
    ], "Okay, What do you want to do with config?", 2)


def _send_inline_keyboard_markup(bot: Bot,
                                 button_list=[],
                                 message_text=None,
                                 n_cols=1,
                                 edit_message_query=None):
    """
    Create an inline keyboard markup to prompt user with different options
    """
    reply_markup = InlineKeyboardMarkup(build_menu(button_list, n_cols=n_cols))
    chat_id = int(_CONF['telegram']['chat_id'])
    if edit_message_query is None:
        bot.send_message(chat_id=chat_id, text=message_text,
                         reply_markup=reply_markup)
    else:
        bot.edit_message_text(text=message_text,
                              chat_id=edit_message_query.message.chat_id,
                              message_id=edit_message_query.message.message_id,
                              reply_markup=reply_markup)


def request_input(key: str, title: str, conv: Conversation):
    global _CONVERSATION
    send_msg("Okay, give me new value for {}.\n"
             "Current value for {} is <b>{}</b>"
             .format(title, title, _CONF[key]), parse_mode=ParseMode.HTML)
    _CONVERSATION = conv
    return MESSAGE_HANDLER


def _callback(bot, update):
    """
    Handle callbacks for inline keyboard button taps/clicks
    """
    query = update.callback_query
    callback_data = format(query.data)
    global _UPDATED_COINS
    if callback_data == 'view_config':
        # Collect editable config data and send it across in tabular format
        (error, df_pairs) = rpc_config(_CONF)
        if error:
            send_msg(df_pairs, bot=bot)
        else:
            list_type = "Whitelisted" if get_list_type() == ListType.STATIC else "Blacklisted"
            message = tabulate(df_pairs, tablefmt='grid',
                               showindex=False, stralign="center")
            message = ("∙ <b>Max Open Trades:</b> {}\n"
                       "∙ <b>Stake Amount:</b> {} {}\n"
                       "∙ <b>{} Currencies:</b>\n"
                       "<pre>{}</pre>"
                       .format(_CONF['max_open_trades'],
                               _CONF['stake_amount'],
                               _CONF['stake_currency'],
                               list_type,
                               message)
                       )
            bot.edit_message_text(text=message,
                                  chat_id=query.message.chat_id,
                                  message_id=query.message.message_id,
                                  parse_mode=ParseMode.HTML)
    elif callback_data == 'edit_config':
        # Prompt user to pick specific field to edit
        _send_inline_keyboard_markup(bot, [
            InlineKeyboardButton("Edit Max Open Trades",
                                 callback_data="edit_max_open_trades"),
            InlineKeyboardButton("Edit Stake Amount",
                                 callback_data="edit_stake_amount"),
            InlineKeyboardButton("Edit Pair {}".format("Whitelist" if get_list_type(
            ) == ListType.STATIC else "Blacklist"), callback_data="edit_pairs"),
        ], "Select your action", 1, query)
    elif callback_data == 'edit_max_open_trades':
        return request_input('max_open_trades', 'max open trades', Conversation.MAX_OPEN_TRADES)
    elif callback_data == 'edit_stake_amount':
        return request_input('stake_amount', 'stake amount', Conversation.STAKE_AMOUNT)
    elif callback_data == 'edit_pairs':
        # Prompt user to choose whether to delete or add new coins
        list_to_scan = _CONF['exchange']['pair_whitelist'] if get_list_type(
        ) == ListType.STATIC else _CONF['exchange']['pair_blacklist']
        for pair in list_to_scan:
            coin = pair.split("_", 1)[1]
            _UPDATED_COINS.append(coin)
        _send_coins_for_deletion(
            bot,
            "∙ Tap on coin to remove from the list.\n"
            "∙ Send coin to add to the list.\n"
            "∙ Type and send 'Done' when you are finished to save your changes.\n", query
        )
    elif "x_" in callback_data:
        coin = callback_data.split("_", 1)[1]
        if coin in _UPDATED_COINS:
            _UPDATED_COINS.remove(coin)
            list_type = "Whitelist" if get_list_type() == ListType.STATIC else "Blacklist"
            _send_coins_for_deletion(
                bot,
                "✔ Removed {} from {}.\n\n"
                "∙ Tap on coin to remove from the list.\n"
                "∙ Send coin to add to the list.\n"
                "∙ Type and send 'Done' when you are finished to save your changes.\n"
                .format(coin.upper(), list_type), query)
        else:
            send_msg(
                "{} has already been removed from the list".format(coin.upper()))


def _send_coins_for_deletion(bot: Bot, message: str, query=None):
    global _CONVERSATION
    _CONVERSATION = Conversation.UPDATE_COINS
    buttons_list = []
    for coin in _UPDATED_COINS:
        buttons_list.append(InlineKeyboardButton(
            coin, callback_data="x_{}".format(coin)))
    _send_inline_keyboard_markup(bot, buttons_list, message, 3, query)


def _message_handler(bot: Bot, update: Update):
    global _CONVERSATION
    if _CONVERSATION == Conversation.MAX_OPEN_TRADES:
        try:
            new_max_open_trades = int(update.message.text)
        except ValueError:
            send_msg(
                "I don't understand that. Please ensure that you are entering a valid number.")
            return
        _CONF['max_open_trades'] = new_max_open_trades
        _process_config_update()
    elif _CONVERSATION == Conversation.STAKE_AMOUNT:
        try:
            new_stake_amount = float(update.message.text)
        except ValueError:
            send_msg(
                "I don't understand that. Please ensure that you are entering a valid amount.")
            return
        _CONF['stake_amount'] = new_stake_amount
        _process_config_update()
    elif _CONVERSATION == Conversation.UPDATE_COINS:
        _handle_coin_updation(bot, update)


def _process_config_update():
    global _CONVERSATION
    update_config(_CONF)
    send_msg("Success! Please wait while I am saving these changes to config file...")
    _CONVERSATION = Conversation.IDLE


def _handle_coin_updation(bot: Bot, update: Update):
    user_text = update.message.text
    stake_currency = _CONF['stake_currency']
    if user_text.upper() == 'DONE':
        new_list = []
        for coin in _UPDATED_COINS:
            new_list.append("{}_{}".format(stake_currency, coin))
        list_to_update = 'pair_blacklist'
        if get_list_type() == ListType.STATIC:
            list_to_update = 'pair_whitelist'
        _CONF['exchange'][list_to_update] = new_list
        _process_config_update()
        _UPDATED_COINS.clear()
    else:
        coin = user_text.upper()
        list_name = "Whitelist" if get_list_type() == ListType.STATIC else "Blacklist"
        if coin in _UPDATED_COINS:
            send_msg("{} is already added to {}".format(coin, list_name))
        else:
            try:
                exchange.validate_pairs(
                    ["{}_{}".format(_CONF['stake_currency'], coin)])
            except OperationalException as e:
                _send_coins_for_deletion(
                    bot,
                    "✖ Failure! {}\n\n"
                    "∙ Tap on coin to remove from the list.\n"
                    "∙ Send coin to add to the list.\n"
                    "∙ Type and send 'Done' when you are finished to save your changes.\n"
                    .format(e))
                return
            _UPDATED_COINS.append(coin)
            _send_coins_for_deletion(
                bot,
                "✔ Added {} to {}.\n\n"
                "∙ Tap on coin to remove from the list.\n"
                "∙ Send coin to add to the list.\n"
                "∙ Type and send 'Done' when you are finished to save your changes.\n"
                .format(coin.upper(), list_name))


@authorized_only
def _status_table(bot: Bot, update: Update) -> None:
    """
    Handler for /status table.
    Returns the current TradeThread status in table format
    :param bot: telegram bot
    :param update: message update
    :return: None
    """
    # Fetch open trade
    (err, df_statuses) = rpc_status_table()
    if err:
        send_msg(df_statuses, bot=bot)
    else:
        message = tabulate(df_statuses, headers='keys', tablefmt='simple')
        message = "<pre>{}</pre>".format(message)

        send_msg(message, parse_mode=ParseMode.HTML)


@authorized_only
def _daily(bot: Bot, update: Update) -> None:
    """
    Handler for /daily <n>
    Returns a daily profit (in BTC) over the last n days.
    :param bot: telegram bot
    :param update: message update
    :return: None
    """
    try:
        timescale = int(update.message.text.replace('/daily', '').strip())
    except (TypeError, ValueError):
        timescale = 7
    (error, stats) = rpc_daily_profit(timescale,
                                      _CONF['stake_currency'],
                                      _CONF['fiat_display_currency'])
    if error:
        send_msg(stats, bot=bot)
    else:
        stats = tabulate(stats,
                         headers=[
                             'Day',
                             'Profit {}'.format(_CONF['stake_currency']),
                             'Profit {}'.format(_CONF['fiat_display_currency'])
                         ],
                         tablefmt='simple')
        message = '<b>Daily Profit over the last {} days</b>:\n<pre>{}</pre>'.format(
                  timescale, stats)
        send_msg(message, bot=bot, parse_mode=ParseMode.HTML)


@authorized_only
def _profit(bot: Bot, update: Update) -> None:
    """
    Handler for /profit.
    Returns a cumulative profit statistics.
    :param bot: telegram bot
    :param update: message update
    :return: None
    """
    (error, stats) = rpc_trade_statistics(_CONF['stake_currency'],
                                          _CONF['fiat_display_currency'])
    if error:
        send_msg(stats, bot=bot)
        return

    # Message to display
    markdown_msg = """
*ROI:* Close trades
  ∙ `{profit_closed_coin:.8f} {coin} ({profit_closed_percent:.2f}%)`
  ∙ `{profit_closed_fiat:.3f} {fiat}`
*ROI:* All trades
  ∙ `{profit_all_coin:.8f} {coin} ({profit_all_percent:.2f}%)`
  ∙ `{profit_all_fiat:.3f} {fiat}`

*Total Trade Count:* `{trade_count}`
*First Trade opened:* `{first_trade_date}`
*Latest Trade opened:* `{latest_trade_date}`
*Avg. Duration:* `{avg_duration}`
*Best Performing:* `{best_pair}: {best_rate:.2f}%`
    """.format(
        coin=_CONF['stake_currency'],
        fiat=_CONF['fiat_display_currency'],
        profit_closed_coin=stats['profit_closed_coin'],
        profit_closed_percent=stats['profit_closed_percent'],
        profit_closed_fiat=stats['profit_closed_fiat'],
        profit_all_coin=stats['profit_all_coin'],
        profit_all_percent=stats['profit_all_percent'],
        profit_all_fiat=stats['profit_all_fiat'],
        trade_count=stats['trade_count'],
        first_trade_date=stats['first_trade_date'],
        latest_trade_date=stats['latest_trade_date'],
        avg_duration=stats['avg_duration'],
        best_pair=stats['best_pair'],
        best_rate=stats['best_rate']
    )
    send_msg(markdown_msg, bot=bot)


@authorized_only
def _balance(bot: Bot, update: Update) -> None:
    """
    Handler for /balance
    """
    (error, result) = rpc_balance(_CONF['fiat_display_currency'])
    if error:
        send_msg('`All balances are zero.`')
        return

    (currencys, total, symbol, value) = result
    output = ''
    for currency in currencys:
        output += """*Currency*: {currency}
*Available*: {available}
*Balance*: {balance}
*Pending*: {pending}
*Est. BTC*: {est_btc: .8f}
""".format(**currency)

    output += """*Estimated Value*:
*BTC*: {0: .8f}
*{1}*: {2: .2f}
""".format(total, symbol, value)
    send_msg(output)


@authorized_only
def _start(bot: Bot, update: Update) -> None:
    """
    Handler for /start.
    Starts TradeThread
    :param bot: telegram bot
    :param update: message update
    :return: None
    """
    (error, msg) = rpc_start()
    if error:
        send_msg(msg, bot=bot)


@authorized_only
def _stop(bot: Bot, update: Update) -> None:
    """
    Handler for /stop.
    Stops TradeThread
    :param bot: telegram bot
    :param update: message update
    :return: None
    """
    (error, msg) = rpc_stop()
    send_msg(msg, bot=bot)


# FIX: no test for this!!!!
@authorized_only
def _forcesell(bot: Bot, update: Update) -> None:
    """
    Handler for /forcesell <id>.
    Sells the given trade at current price
    :param bot: telegram bot
    :param update: message update
    :return: None
    """

    trade_id = update.message.text.replace('/forcesell', '').strip()
    (error, message) = rpc_forcesell(trade_id)
    if error:
        send_msg(message, bot=bot)
        return


@authorized_only
def _performance(bot: Bot, update: Update) -> None:
    """
    Handler for /performance.
    Shows a performance statistic from finished trades
    :param bot: telegram bot
    :param update: message update
    :return: None
    """
    (error, trades) = rpc_performance()
    if error:
        send_msg(trades, bot=bot)
        return

    stats = '\n'.join('{index}.\t<code>{pair}\t{profit:.2f}% ({count})</code>'.format(
        index=i + 1,
        pair=trade['pair'],
        profit=trade['profit'],
        count=trade['count']
    ) for i, trade in enumerate(trades))
    message = '<b>Performance:</b>\n{}'.format(stats)
    send_msg(message, parse_mode=ParseMode.HTML)


@authorized_only
def _count(bot: Bot, update: Update) -> None:
    """
    Handler for /count.
    Returns the number of trades running
    :param bot: telegram bot
    :param update: message update
    :return: None
    """
    (error, trades) = rpc_count()
    if error:
        send_msg(trades, bot=bot)
        return

    message = tabulate({
        'current': [len(trades)],
        'max': [_CONF['max_open_trades']]
    }, headers=['current', 'max'], tablefmt='simple')
    message = "<pre>{}</pre>".format(message)
    logger.debug(message)
    send_msg(message, parse_mode=ParseMode.HTML)


@authorized_only
def _help(bot: Bot, update: Update) -> None:
    """
    Handler for /help.
    Show commands of the bot
    :param bot: telegram bot
    :param update: message update
    :return: None
    """
    message = """
*/start:* `Starts the trader`
*/stop:* `Stops the trader`
*/status [table]:* `Lists all open trades`
            *table :* `will display trades in a table`
*/profit:* `Lists cumulative profit from all finished trades`
*/forcesell <trade_id>|all:* `Instantly sells the given trade or all trades, regardless of profit`
*/performance:* `Show performance of each finished trade grouped by pair`
*/daily <n>:* `Shows profit or loss per day, over the last n days`
*/count:* `Show number of trades running compared to allowed number of trades`
*/balance:* `Show account balance per currency`
*/help:* `This help message`
*/version:* `Show version`
    """
    send_msg(message, bot=bot)


@authorized_only
def _version(bot: Bot, update: Update) -> None:
    """
    Handler for /version.
    Show version information
    :param bot: telegram bot
    :param update: message update
    :return: None
    """
    send_msg('*Version:* `{}`'.format(__version__), bot=bot)


def build_menu(buttons,
               n_cols,
               header_buttons=None,
               footer_buttons=None):
    menu = [buttons[i:i + n_cols] for i in range(0, len(buttons), n_cols)]
    if header_buttons:
        menu.insert(0, header_buttons)
    if footer_buttons:
        menu.append(footer_buttons)
    return menu


def send_msg(msg: str, bot: Bot = None, parse_mode: ParseMode = ParseMode.MARKDOWN) -> None:
    """
    Send given markdown message
    :param msg: message
    :param bot: alternative bot
    :param parse_mode: telegram parse mode
    :return: None
    """
    if not is_enabled():
        return

    bot = bot or _UPDATER.bot

    keyboard = [['/daily', '/profit', '/balance', '/config'],
                ['/status', '/status table', '/performance'],
                ['/count', '/start', '/stop', '/help']]

    reply_markup = ReplyKeyboardMarkup(keyboard)

    try:
        try:
            bot.send_message(
                _CONF['telegram']['chat_id'], msg,
                parse_mode=parse_mode, reply_markup=reply_markup
            )
        except NetworkError as network_err:
            # Sometimes the telegram server resets the current connection,
            # if this is the case we send the message again.
            logger.warning(
                'Telegram NetworkError: %s! Trying one more time.',
                network_err.message
            )
            bot.send_message(
                _CONF['telegram']['chat_id'], msg,
                parse_mode=parse_mode, reply_markup=reply_markup
            )
    except TelegramError as telegram_err:
        logger.warning(
            'TelegramError: %s! Giving up on that message.', telegram_err.message)
