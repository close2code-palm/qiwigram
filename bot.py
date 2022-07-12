import uuid

import asyncpg
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
dp = Dispatcher(bot)

p2p = AioQiwiP2P(auth_key=QIWI_KEY)

@dp.message_handler(commands=['start'])
async def start_conversation(message: types.Message):
    message_to_user = '''Привет, {}!\n\nЯ - бот для пополнения баланса. 
Нажмите на кнопку, чтобы пополнить баланс
Снизу инлайн кнопка с текстом "Пополнить баланс"'''

    pay_inline_mu = types.InlineKeyboardMarkup().add(
        types.InlineKeyboardButton("Пополнить баланс", callback_data='pay_cb_data'))
    await message.reply(message_to_user.format(
        message.from_user.username), reply_markup=pay_inline_mu)  # possible empty username


class Payment(StatesGroup):
    payment_amount = State()


@dp.callback_query_handler(text='pay_cb_data')
async def inline_btn_cb_handler(query: types.CallbackQuery):
    await query.answer('Формируем заявку...')
    await Payment.payment_amount.set()
    await bot.send_message(query.from_user.id,
                           'Введите сумму, на которую вы хотите пополнить баланс')


async def write_payment(user_id, amount):
    conn = await asyncpg.connect(DSN)
    on_start_query = """CREATE TABLE IF NOT EXISTS 
         bot.clients(user_id BIGINT UNIQUE, amount INTEGER)"""
    get_current_amount_query = """SELECT amount FROM clients WHERE user_id = $1"""
    write_new_amount = """UPDATE clients SET amount = $1 WHERE user_id = $2"""
    users_balance = await conn.fetchval(get_current_amount_query, user_id)  # no user?
    balance_with_accepted = users_balance + amount
    await conn.execute(write_new_amount, balance_with_accepted, user_id)
    await conn.close()


@dp.message_handler(lambda message: message.text.isdigit(), state=Payment.payment_amount)
async def process_sum(message: types.Message, state: FSMContext):
    new_bill = await p2p.bill(bill_id=str(uuid.uuid5(uuid.NAMESPACE_X500, str(message.from_id))),
                              amount=message.text, lifetime=5)
    async with state.proxy() as payment_data:
        payment_data['amount'] = int(message.text)
        payment_data['bill_id'] = new_bill.bill_id
    await state.finish()
    payment_created_kb = types.InlineKeyboardMarkup().add(
        types.InlineKeyboardButton("Перейти к оплате", url=f'{new_bill.pay_url}')
    ).add(
        types.InlineKeyboardButton("Оплачено!", callback_data='check_qiwi')
    )
    message_id = await message.reply('Платёж успешно создан!', reply_markup=payment_created_kb)


@dp.callback_query_handler(text='check_qiwi')
async def check_qiwi(cb: types.CallbackQuery, state: FSMContext):
    await cb.answer('Проверка оплаты')
    user_id = cb.from_user.id
    async with state.proxy as payment_data:
        bill_id = payment_data['bill_id']
    payment_status = (await p2p.check(bill_id=bill_id)).status
    match payment_status:
        case['WAITING']:
            await bot.send_message(user_id, "Сервис ожидает оплату")
        case['PAID']:
            async with state.proxy() as payment_data:
                bill_amount = payment_data['amount']
            await write_payment(user_id, bill_amount)
        case['REJECTED']:
            ...
        case['EXPIRED']:


@dp.message_handler(lambda message: not message.text.isdigit(), state=Payment.payment_amount)
async def process_sum_invalid(message: types.Message, state: FSMContext):
    return await message.reply('Cумма должна быть числом\nВведите сумму, '
                               'на которую вы хотите пополнить баланс')


if __name__ == '__main__':
    executor.start_polling(dp, skip_updates=True)
