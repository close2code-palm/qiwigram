"""Single-file bot script for accepting payments with QIWI"""
import logging
import uuid

import asyncpg
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import StatesGroup, State
from aiogram.utils import executor
from environs import Env
from pyqiwip2p import AioQiwiP2P

env = Env()
env.read_env()
DSN = env.str('DSN')
TG_API_KEY = env.str('TG_API_KEY')
QIWI_KEY = env.str('QIWI_KEY')

logging.basicConfig(level=logging.INFO)

bot = Bot(token=TG_API_KEY)
storage = MemoryStorage()

dp = Dispatcher(bot, storage=storage)

p2p = AioQiwiP2P(auth_key=QIWI_KEY)


@dp.message_handler(commands=['start'])
async def start_conversation(message: types.Message):
    """Client conversation entry point"""
    message_to_user = '''Привет, {}!\n\nЯ - бот для пополнения баланса.
    Нажмите на кнопку, чтобы пополнить баланс'''
    pay_inline_mu = types.InlineKeyboardMarkup().add(
        types.InlineKeyboardButton("Пополнить баланс", callback_data='pay_cb_data'))
    application_to_user = message.from_user.username if message.from_user.username \
        else 'пользователь'
    logging.log(logging.DEBUG, 'New converstation started')
    await message.reply(message_to_user.format(
        application_to_user), reply_markup=pay_inline_mu)


@dp.message_handler(commands=['admin'])
async def admin_handler(message: types.Message):
    """Admin entry point"""
    logging.warning('Admin panel enter')
    await message.reply('Реализация не за горами')


class Payment(StatesGroup):
    """Dataclass for state machine"""
    payment_amount = State()


@dp.callback_query_handler(text='pay_cb_data')
async def inline_btn_cb_handler(query: types.CallbackQuery):
    """`Пополнить баланс` callback handler, inits state machine"""
    await query.answer('Формируем заявку...')
    logging.debug('Payment creation request, getting amount')
    await Payment.payment_amount.set()
    await bot.send_message(query.from_user.id,
                           'Введите сумму, на которую вы хотите пополнить баланс')


async def write_payment(user_id, amount):
    """Function handles accepted payment - writes changes to database"""
    conn = await asyncpg.connect(DSN)
    get_current_amount_query = """SELECT amount FROM clients WHERE user_id = $1"""
    write_new_amount = """UPDATE clients SET amount = $1 WHERE user_id = $2"""
    try:
        users_balance = await conn.fetchval(get_current_amount_query, user_id)
        if not users_balance:  # None if value not found
            users_balance = 0
        balance_with_accepted = users_balance + amount
        await conn.execute(write_new_amount, balance_with_accepted, user_id)
        logging.info('Payment %s for user %s, balance updated' % (amount, user_id))
    except Exception as e:
        logging.error('Database error on write of payment of %s'
                      ' RUB for %s, %s' % (amount, user_id, e))
    finally:
        await conn.close()


async def db_on_startup():
    """Checks payments table in database, introduced in
    DSN variable of .env file"""
    conn = await asyncpg.connect(DSN)
    on_start_query = """CREATE TABLE IF NOT EXISTS
         clients(user_id BIGINT UNIQUE, amount INTEGER)"""
    try:
        await conn.execute(on_start_query)
    except Exception as e:
        logging.error('Error on get_or_create table: %s' % e)
    finally:
        await conn.close()


@dp.message_handler(lambda message: message.text.isdigit(), state=Payment.payment_amount)
async def process_sum(message: types.Message, state: FSMContext):
    """Gets the amount of money for payment and creates bill with it as parameter"""
    msg_from_id = message.from_id
    new_bill = await p2p.bill(bill_id=str(uuid.uuid4()),  # or uuid5 with salt
                              amount=message.text, lifetime=5)
    async with state.proxy() as payment_data:
        payment_data['amount'] = int(message.text)
        payment_data['bill_id'] = new_bill.bill_id
    logging.info('Created QIWI bill by %s' % msg_from_id)
    await Payment.next()
    payment_created_kb = types.InlineKeyboardMarkup().add(
        types.InlineKeyboardButton("Перейти к оплате", url=f'{new_bill.pay_url}')
    ).add(
        types.InlineKeyboardButton("Оплачено!", callback_data='check_qiwi')
    )
    await message.reply('Платёж успешно создан!', reply_markup=payment_created_kb)


@dp.callback_query_handler(text='check_qiwi')
async def check_qiwi(cb_query: types.CallbackQuery, state: FSMContext):
    """On `PAID` button click checks the pyament status through
    qiwi p2p api and handles the result"""
    await cb_query.answer('Проверка оплаты')
    user_id = cb_query.from_user.id
    logging.info('User %s requested bill status' % user_id)
    async with state.proxy() as payment_data:
        bill_id = payment_data['bill_id']
    payment_status = (await p2p.check(bill_id=bill_id)).status
    match payment_status:
        case 'WAITING':
            await bot.send_message(user_id, "Сервис ожидает оплату")
        case 'PAID':
            async with state.proxy() as payment_data:
                bill_amount = payment_data['amount']
                await write_payment(user_id, bill_amount)
            await state.finish()
            await bot.send_message(user_id, 'Оплата успешно обработана!')
        case 'REJECTED':
            await bot.send_message(user_id, "Платёж отменён")
            await state.finish()
        case 'EXPIRED':
            await bot.send_message(user_id,
                                   'Срок действия платежа истёк')
            await state.finish()


@dp.message_handler(commands='cancel')
async def cancel_bill(message: types.Message, state: FSMContext):
    """In case of wrong amount or some other situations"""
    async with state.proxy() as payment_data:
        if not payment_data:
            return
        bill_id = payment_data['bill_id']
    await state.finish()
    await p2p.reject(bill_id=bill_id)
    logging.info('Payment %s cancelled' % bill_id)
    await message.reply('Платёж анулирован')


@dp.message_handler(lambda message: not message.text.isdigit(), state=Payment.payment_amount)
async def process_sum_invalid(message: types.Message, _: FSMContext):
    """Works in case of incorrect unprocessable data for ayment"""
    logging.debug('Wrong amount entered by %s' % message.from_user.id)
    return await message.reply('Cумма должна быть числом\nВведите сумму, '
                               'на которую вы хотите пополнить баланс')


if __name__ == '__main__':
    executor.start(dp, db_on_startup())  # Checks database
    executor.start_polling(dp, skip_updates=True)
