"""
QR-code rendering for the Telegram "link with a QR code" flow (api/routes/telegram.py).

Why the server draws the image
------------------------------
The text inside the QR code is a Telegram login token (`tg://login?token=...`): whoever scans it with a logged-in Telegram app authorises the session that requested it.
Rendering the picture here, with the small pure-Python `qrcode` package, means no third-party JavaScript has to be shipped,
vendored and kept up to date in web/, and nothing sensitive is ever handed to an external QR service.
The browser only ever receives the finished picture, as a `data:` URI it can drop straight into an <img>.

Why SVG: it is vector (sharp at any size), needs no imaging library such as Pillow, and an <img> never executes anything inside an SVG,
so even a hypothetical bad payload could not run script in the page.
"""

import base64
import io

import qrcode
import qrcode.image.svg


def qr_data_uri(text: str) -> str:
    """
    Render `text` as a QR code and return it as a `data:image/svg+xml;base64,...` URI.

    The quiet zone (`border=4`, the QR specification's minimum) matters: without that white margin many phone cameras fail to lock on,
    which is why the front end shows the image on a white background regardless of the page theme.
    The token is never logged or stored by this function - it only turns a string into a picture.
    """
    image = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=4)
    buffer = io.BytesIO()
    image.save(buffer)
    return "data:image/svg+xml;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")