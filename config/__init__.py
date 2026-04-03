"""
ocpp-charge-point-simulator — Configuration
All tuneable parameters are defined here.
Edit this file to match your CSMS setup.
"""

# ─── CSMS Connection ─────────────────────────────────────────────────────────

CSMS_URL      = "wss://hasan-7fap.powerfill.app/ws/CP-1"
CHARGE_BOX_ID = "CP-1"

# ─── Charge Point Identity ───────────────────────────────────────────────────

VENDOR   = "TestVendor"
MODEL    = "TestModel"
SERIAL   = "SN-001"
FIRMWARE = "1.0.0"

# ─── Authentication ──────────────────────────────────────────────────────────

DEFAULT_ID_TAG      = "hasanyildizidtag"
BASIC_AUTH_USER     = "CP-1"
BASIC_AUTH_PASSWORD = "1234567890asdfgh"

# ─── NFC Authentication ──────────────────────────────────────────────────────
# Simülatör başlamadan önce NFC kart doğrulaması aktiftir.
# Sadece NFC_ALLOWED_ID ile eşleşen kart okutulduğunda simülasyon başlar.

NFC_ALLOWED_ID = "97350E07"   # İzin verilen NFC kart UID (büyük harf, boşluksuz)

# ─── Heartbeat ───────────────────────────────────────────────────────────────

HEARTBEAT_INTERVAL = 30  # seconds (overridden by BootNotification response)

# ─── Meter Simulation ────────────────────────────────────────────────────────

METER_INCREMENT_WH = 500   # Wh added on every MeterValues call  →  %5 = 500 Wh
DEFAULT_VOLTAGE    = 230   # V
DEFAULT_CURRENT    = 16    # A

# ─── OCPP Protocol ───────────────────────────────────────────────────────────

SUBPROTOCOL   = "ocpp1.6"
PING_INTERVAL = None  # Managed by our own heartbeat loop

# ─── Nextion Display ─────────────────────────────────────────────────────────

NEXTION_PORT     = "/dev/ttyAMA0"   # GPIO 14 (TX) / GPIO 15 (RX)
NEXTION_BAUDRATE = 9600

# ─── Nextion Picture IDs ─────────────────────────────────────────────────────
# Görseller ID:
#   0  → yeşil araç (bağlı)
#   3  → kırmızı araç (bağlı değil)
#   5,6,7 → RFID animasyon kareleri (rfid_scan sayfası)

PIC_CAR_CONNECTED    = 0
PIC_CAR_DISCONNECTED = 3
PIC_RFID_FRAMES      = [5, 6, 7]

# ─── Nextion Renk Kodları (RGB565) ───────────────────────────────────────────
# con.pco renk değerleri:
#   NOT CONNECTED  → 63488   (kırmızı   0xF800)
#   AVAILABLE      →  2047   (cyan      0x07FF)
#   CHARGING       → 11939   (yeşil     0x2EA3)

# ─── Şarj / Ücretlendirme ────────────────────────────────────────────────────
# 500 Wh  → %5 şarj artışı
# 500 Wh  → 5 TL

WH_PER_STEP        = 500    # Her MeterValues çağrısında eklenen Wh
PERCENT_PER_STEP   = 5      # Her adımdaki şarj yüzdesi artışı
TL_PER_500WH       = 5.0    # 500 Wh başına ücret (TL)
