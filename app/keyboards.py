from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from .indexer import small_caps

def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=small_caps(t), callback_data=d) for t, d in row]
        for row in rows
    ])

def main():
    return kb([
        [('🔗 Connect Telegram Account','connect')],
        [('📡 My Channels','channels'),('➕ Add Channel','add_channel')],
        [('🤖 Auto Index','quick:auto'),('📝 Manual Index','quick:manual')],
        [('📂 My Indexing Jobs','jobs')],
        [('👤 My Account','account'),('❓ Help','help')],
    ])

def back(target='home'):
    return kb([[('⬅️ Back','back:'+target)],[('🏠 Main Menu','home')]])

def connect():
    return kb([[('🔐 Connect Account','login')],[('📊 Account Status','status')],[('🔌 Disconnect Account','disconnect')],[('⬅️ Back to Main Menu','home')]])

def status():
    return kb([[('🔄 Refresh Status','status')],[('🔌 Disconnect Account','disconnect')],[('⬅️ Back','connect')],[('🏠 Main Menu','home')]])

def modes():
    return kb([[('📝 Manual Template Mode','mode:manual')],[('🤖 Automatic Topic Mode','mode:auto')],[('⬅️ Back to Channel Selection','create')]])

def channel_rows(channels, prefix='select'):
    rows=[[(('📚 '+c['title'])[:60],f'{prefix}:{c["channel_id"]}')] for c in channels]
    rows.append([('⬅️ Back to Main Menu','home')])
    return kb(rows)
