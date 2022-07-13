import uuid

import asyncpg
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import StatesGroup, State
from aiogram.utils import executor
from environs import Env
from aiogram import Bot, Dispatcher, types
from pyqiwip2p import AioQiwiP2P

env = Env()
env.read_env()
DSN = env.str('DSN')
TG_API_KEY = env.str('TG_API_KEY')
QIWI_KEY = env.str('QIWI_KEY')

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
    application_to_user = message.from_user.username if message.from_user.username else 'пользователь'
    await message.reply(message_to_user.format(
        application_to_user), reply_markup=pay_inline_mu)


@dp.message_handler(commands=['admin'])
async def admin_handler(message: types.Message):
    """Admin entry point"""
    ...


class Payment(StatesGroup):
    """Dataclass for state machine"""
    payment_amount = State()


@dp.callback_query_handler(text='pay_cb_data')
async def inline_btn_cb_handler(query: types.CallbackQuery):
    """`Пополнить баланс` callback handler, inits state machine"""
    await query.answer('Формируем заявку...')
    await Payment.payment_amount.set()
    await bot.send_message(query.from_user.id,
                           'Введите сумму, на которую вы хотите пополнить баланс')


async def write_payment(user_id, amount):
    """Function handles accepted payment - writes changes to database"""
    conn = await asyncpg.connect(DSN)
    get_current_amount_query = """SELECT amount FROM clients WHERE user_id = $1"""
    write_new_amount = """UPDATE clients SET amount = $1 WHERE user_id = $2"""
    users_balance = await conn.fetchval(get_current_amount_query, user_id)
    if not users_balance:  # None if value not found
        users_balance = 0
    balance_with_accepted = users_balance + amount
    await conn.execute(write_new_amount, balance_with_accepted, user_id)
    await conn.close()


async def db_on_startup():
    """Checks payments table in database, introduced in
    DSN variable of .env file"""
    conn = await asyncpg.connect(DSN)
    on_start_query = """CREATE TABLE IF NOT EXISTS 
         clients(user_id BIGINT UNIQUE, amount INTEGER)"""
    await conn.execute(on_start_query)
    await conn.close()


@dp.message_handler(lambda message: message.text.isdigit(), state=Payment.payment_amount)
async def process_sum(message: types.Message, state: FSMContext):
    """Gets the amount of money for payment and creates bill with it as parameter"""
    new_bill = await p2p.bill(bill_id=str(uuid.uuid5(uuid.NAMESPACE_X500, str(message.from_id))),
                              amount=message.text, lifetime=5)
    async with state.proxy() as payment_data:
        payment_data['amount'] = int(message.text)
        payment_data['bill_id'] = new_bill.bill_id
    await Payment.next()
    payment_created_kb = types.InlineKeyboardMarkup().add(
        types.InlineKeyboardButton("Перейти к оплате", url=f'{new_bill.pay_url}')
    ).add(
        types.InlineKeyboardButton("Оплачено!", callback_data='check_qiwi')
    )
    await message.reply('Платёж успешно создан!', reply_markup=payment_created_kb)


@dp.callback_query_handler(text='check_qiwi')
async def check_qiwi(cb: types.CallbackQuery, state: FSMContext):
    """On `PAID` button click checks the pyament status through
    qiwi p2p api and handles the result"""
    await cb.answer('Проверка оплаты')
    user_id = cb.from_user.id
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
        case 'REJECTED':
            await bot.send_message(user_id, "Платёж отменён")
            await state.finish()
        case 'EXPIRED':
            await bot.send_message(user_id,
                                   'Срок действия платежа истёк')
            await state.finish()


@dp.message_handler(lambda message: not message.text.isdigit(), state=Payment.payment_amount)
async def process_sum_invalid(message: types.Message, state: FSMContext):
    """Works in case of incorrect unprocessable data for ayment"""
    return await message.reply('Cумма должна быть числом\nВведите сумму, '
                               'на которую вы хотите пополнить баланс')


if __name__ == '__main__':
    executor.start(dp, db_on_startup())  # Checks database
    executor.start_polling(dp, skip_updates=True)
