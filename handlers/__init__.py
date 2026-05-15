"""
handlers - Telethon event handler package.

Each module in this package corresponds to one Telegram event type:

    on_message.py   ->  NewMessage
    on_delete.py    ->  MessageDeleted
    on_edit.py      ->  MessageEdited

Handlers are registered in main.py via register_handlers(). Nothing is
imported here automatically - Telethon decorators only activate when the
module containing them is imported, so main.py controls that explicitly.
"""