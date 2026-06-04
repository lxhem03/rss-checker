from pyrogram import Client

from .start    import register as reg_start
from .download import register as reg_download
from .rssfeed  import register as reg_rssfeed
from .feeds    import register as reg_feeds
from .status   import register as reg_status
from .cancel   import register as reg_cancel
from .settings import register as reg_settings


def register_handlers(app: Client) -> None:
    reg_start(app)
    reg_download(app)
    reg_rssfeed(app)
    reg_feeds(app)
    reg_status(app)
    reg_cancel(app)
    reg_settings(app)
