import base64
import io

import qrcode
from qrcode.image.pil import PilImage

from app.config import settings


def build_upi_uri(
    amount: float,
    transaction_id: str,
    note: str | None = None,
    vpa: str | None = None,
    display_name: str | None = None,
) -> str:
    """
    Constructs a UPI deep-link URI.
    UPI spec: upi://pay?pa=<VPA>&pn=<Name>&am=<Amount>&cu=INR&tn=<Note>&tr=<TxnRef>
    vpa / display_name override the global defaults from settings per transaction.
    """
    params = {
        "pa": vpa or settings.UPI_VPA,
        "pn": display_name or settings.UPI_DISPLAY_NAME,
        "am": f"{amount:.2f}",
        "cu": "INR",
        "tr": transaction_id,
    }
    if note:
        params["tn"] = note[:50]

    query = "&".join(f"{k}={v}" for k, v in params.items())
    return f"upi://pay?{query}"


def generate_qr_base64(upi_uri: str) -> str:
    """Returns a base64-encoded PNG of the UPI QR code."""
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=4,
    )
    qr.add_data(upi_uri)
    qr.make(fit=True)

    img: PilImage = qr.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    return base64.b64encode(buffer.read()).decode("utf-8")
