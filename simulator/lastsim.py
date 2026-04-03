#!/usr/bin/env python3
"""
ocpp-charge-point-simulator — OCPP 1.6J + Nextion 3.5" Entegrasyonu
=====================================================================
Raspberry Pi Zero 2W · GPIO 14/15 (UART) · /dev/ttyAMA0 · 9600 baud
Nextion sayfaları: home, user_info, status, rfid_scan

NFC Kimlik Doğrulama:
  Simülatör başlamadan önce PN532 I2C NFC okuyucu ile kart doğrulanır.
  config/__init__.py içindeki NFC_ALLOWED_ID ile eşleşen kart
  okutulduğunda simülasyon başlar.
"""

import asyncio
import json
import signal
import sys
import time
from datetime import datetime, timezone

TZ_UTC = timezone.utc

try:
    import websockets
except ImportError:
    print("[ERROR] 'websockets' eksik. Kur:  pip install websockets")
    sys.exit(1)

try:
    import serial
except ImportError:
    print("[ERROR] 'pyserial' eksik. Kur:  pip install pyserial")
    sys.exit(1)

# ─── Konfigürasyon ───────────────────────────────────────────────────────────

sys.path.insert(0, ".")
from config import (
    CSMS_URL,
    CHARGE_BOX_ID,
    DEFAULT_ID_TAG,
    NFC_ALLOWED_ID,
    VENDOR, MODEL, SERIAL, FIRMWARE,
    HEARTBEAT_INTERVAL,
    METER_INCREMENT_WH,
    DEFAULT_VOLTAGE,
    DEFAULT_CURRENT,
    SUBPROTOCOL,
    PING_INTERVAL,
    BASIC_AUTH_USER,
    BASIC_AUTH_PASSWORD,
    NEXTION_PORT,
    NEXTION_BAUDRATE,
    PIC_CAR_CONNECTED,
    PIC_CAR_DISCONNECTED,
    WH_PER_STEP,
    PERCENT_PER_STEP,
    TL_PER_500WH,
)

import base64

# ─── Runtime State ────────────────────────────────────────────────────────────

msg_id         = 1
transaction_id = None
meter_wh       = 0
hb_interval    = HEARTBEAT_INTERVAL
hb_task        = None

# Şarj durumu
charge_percent    = 0        # %0 - %100
charge_start_time = None     # şarj başladığında set edilir
charging_active   = False    # şarj devam ediyor mu?
total_cost        = 0.0      # toplam ücret (TL)
total_energy_wh   = 0        # toplam çekilen enerji (Wh)

# Bağlantı durumu
is_connected = False

# ─── ANSI Renk Kodları ────────────────────────────────────────────────────────

RESET  = "\033[0m"
BOLD   = "\033[1m"
CYAN   = "\033[36m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
RED    = "\033[31m"
DIM    = "\033[2m"


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


def log(direction: str, msg: str):
    colors = {"SEND": CYAN, "RECV": GREEN, "INFO": DIM, "WARN": YELLOW, "ERR": RED}
    c = colors.get(direction, RESET)
    print(f"{DIM}{_ts()}{RESET}  {c}{BOLD}{direction:<4}{RESET}  {msg}")


# ─── NFC Kimlik Doğrulama ─────────────────────────────────────────────────────

def wait_for_nfc_auth():
    """
    BootNotification gönderilmeden önce NFC kart doğrulaması.

    PN532 (I2C) üzerinden kart okur. config/__init__.py içindeki
    NFC_ALLOWED_ID ile eşleşen kart okutulana kadar simülasyon başlamaz.
    Yanlış kart okutulursa uyarı verir ve tekrar bekler.
    Ctrl+C ile çıkılabilir.
    """
    from nfc_read import init_pn532, read_uid

    print(f"\n{BOLD}{CYAN}╔══════════════════════════════════════╗{RESET}")
    print(f"{BOLD}{CYAN}║     NFC Kimlik Doğrulama             ║{RESET}")
    print(f"{BOLD}{CYAN}╚══════════════════════════════════════╝{RESET}")
    print(f"{DIM}İzin verilen kart UID : {NFC_ALLOWED_ID}{RESET}")
    print(f"{YELLOW}Kartı okuyucuya yaklaştırın...{RESET}\n")

    init_pn532()

    while True:
        try:
            uid = read_uid()
            if uid:
                uid_str = uid.hex().upper()
                if uid_str == NFC_ALLOWED_ID:
                    print(f"{GREEN}{BOLD}✓ Kart onaylandı  : {uid_str}{RESET}")
                    print(f"{GREEN}  Simülasyon başlıyor...{RESET}\n")
                    return  # Doğrulama başarılı → simülasyon devam eder
                else:
                    print(f"{RED}✗ Geçersiz kart   : {uid_str}  —  Tekrar deneyin{RESET}")
            time.sleep(0.1)
        except KeyboardInterrupt:
            print(f"\n{YELLOW}Çıkış yapılıyor...{RESET}")
            sys.exit(0)


# ─── Nextion Serial Bağlantısı ────────────────────────────────────────────────

_nxt_serial = None
_nxt_queue = None   # Nextion yazma kuyruğu (event loop icinde init edilir)


def nextion_open():
    """Serial portu aç. Hata olursa uyar ama çökme."""
    global _nxt_serial
    try:
        _nxt_serial = serial.Serial(
            NEXTION_PORT,
            NEXTION_BAUDRATE,
            timeout=0.1
        )
        # Önceki oturumdan kalan UART tamponunu temizle (crash önleme)
        _nxt_serial.reset_input_buffer()
        _nxt_serial.reset_output_buffer()
        log("INFO", f"Nextion bağlandı → {NEXTION_PORT} @ {NEXTION_BAUDRATE}")
    except Exception as e:
        log("WARN", f"Nextion açılamadı: {e} — ekran komutları devre dışı")
        _nxt_serial = None


def nxt(cmd: str):
    """
    Nextion komutunu yazma kuyruğuna ekle (non-blocking).
    Fiili yazma nxt_writer_loop() tarafından yapılır; komutlar arası 10ms
    gecikme ile seri portu ve Nextion tamponu taşmaz.
    """
    if _nxt_serial is None or _nxt_queue is None:
        return
    try:
        _nxt_queue.put_nowait(cmd)
    except Exception:
        pass  # kuyruk dolu veya event loop yok — komutu sessizce at


async def nxt_writer_loop():
    """
    Nextion seri yazma görevi (arka plan).

    Kuyruktaki komutları sırayla alır, seri porta yazar ve her yazma
    sonrasında 10ms bekler. Bu bekleme Nextion'ın 128-byte UART tamponunun
    taşmasını ve ekranın donup çökmesini önler.
    """
    while True:
        cmd = await _nxt_queue.get()
        if _nxt_serial is not None:
            try:
                _nxt_serial.write((cmd + "\xff\xff\xff").encode("latin-1"))
            except Exception as e:
                log("WARN", f"Nextion yazma hatası: {e}")
        await asyncio.sleep(0.010)   # 10ms: Nextion minimum komutlar arası bekleme
        _nxt_queue.task_done()


# ─── Nextion UI Güncelleme Fonksiyonları ─────────────────────────────────────

def nxt_set_time():
    """home sayfası saat → UTC saati."""
    utc = datetime.now(TZ_UTC).strftime("%H:%M:%S")
    nxt(f'saat.txt="{utc}"')


def nxt_set_status(status: str):
    """
    home sayfası con objesi + araba görseli.

    Durum → Nextion con.txt metni + con.pco rengi + araba görseli:
      NOT CONNECTED : 63488  (kırmızı  0xF800)  — bağlantı yok
      AVAILABLE     :  2047  (cyan     0x07FF)  — hazır, araç yok
      CHARGING      : 11939  (yeşil    0x2EA3)  — şarj ediliyor
      UNAVAILABLE   : 63488  (kırmızı  0xF800)  — çevrimdışı
    """
    colors = {
        "NOT CONNECTED": 63488,   # kırmızı  0xF800
        "AVAILABLE":      2047,   # cyan     0x07FF
        "CHARGING":      11939,   # yeşil    0x2EA3
        "UNAVAILABLE":   63488,   # kırmızı  0xF800
    }
    pic = PIC_CAR_DISCONNECTED if status in ("NOT CONNECTED", "UNAVAILABLE") else PIC_CAR_CONNECTED
    pco = colors.get(status, 63488)
    nxt(f'con.txt="{status}"')
    nxt(f"con.pco={pco}")
    nxt(f"araba.pic={pic}")


def nxt_set_charge_percent(pct: int):
    """home sayfası percent → şarj yüzdesi."""
    nxt(f'percent.txt="% {pct}"')


def nxt_set_user_id(id_tag: str):
    """user_info sayfası id.txt → idTag (config'den okunur, her başlatmada yazılır)."""
    nxt(f'id.txt="{id_tag}"')


async def nextion_read_loop():
    """Nextion'dan gelen buton olaylarını dinle (Touch Event 0x65)."""
    loop = asyncio.get_event_loop()
    buf  = bytearray()
    while True:
        if _nxt_serial is None:
            await asyncio.sleep(0.1)
            continue
        try:
            chunk = await loop.run_in_executor(None, _nxt_serial.read, 32)
            if chunk:
                buf.extend(chunk)
            # 0x65 touch event paketi: [0x65, page, comp, event, 0xFF, 0xFF, 0xFF] = 7 byte
            while len(buf) >= 7:
                idx = buf.find(0x65)
                if idx == -1:
                    buf.clear()
                    break
                if idx > 0:
                    del buf[:idx]
                if len(buf) < 7:
                    break
                if buf[4] == 0xFF and buf[5] == 0xFF and buf[6] == 0xFF:
                    page_id = buf[1]
                    comp_id = buf[2]
                    event   = buf[3]   # 0x01 = press, 0x00 = release
                    del buf[:7]
                    if event == 0x01:
                        log("INFO", f"Nextion touch → page={page_id} comp={comp_id}")
                        if comp_id == 2:   # ← useridtag butonunun component ID'si
                            log("INFO", f"useridtag butonu → id yazılıyor: {DEFAULT_ID_TAG}")
                            nxt_set_user_id(DEFAULT_ID_TAG)
                else:
                    del buf[:1]   # bozuk paket, bir byte atla
        except Exception as e:
            log("WARN", f"Nextion okuma hatası: {e}")
            await asyncio.sleep(0.1)


def nxt_update_status():
    """
    status sayfası güncelle:
      power  → anlık güç (kW) = V × A / 1000, sabit
      time   → geçen süre (HH:MM:SS), her saniye artar
      energy → geçen_saniye × METER_INCREMENT_WH  (Wh)
               Örnek: 10s × 500 = 5000 Wh
      cost   → toplam ücret (TL) = (energy_wh / WH_PER_STEP) × TL_PER_500WH
    Şarj aktif değilse son değerleri dondurur.
    """
    power_kw = round(DEFAULT_VOLTAGE * DEFAULT_CURRENT / 1000, 2)
    nxt(f'power.txt="POWER : {power_kw} KW"')

    if charge_start_time is not None:
        elapsed = int(time.time() - charge_start_time)   # saniye cinsinden
        h = elapsed // 3600
        m = (elapsed % 3600) // 60
        s = elapsed % 60
        nxt(f'time.txt="TIME : {h:02d}:{m:02d}:{s:02d}"')

        # Enerji: geçen_saniye × METER_INCREMENT_WH  (Wh)
        energy_wh = elapsed * METER_INCREMENT_WH
        nxt(f'energy.txt="ENERGY: {energy_wh} Wh"')

        # Ücret: her WH_PER_STEP Wh = TL_PER_500WH TL oranıyla
        cost = round((energy_wh / WH_PER_STEP) * TL_PER_500WH, 2)
        nxt(f'cost.txt="COST : {cost} TL"')
    else:
        nxt('time.txt="TIME : 00:00:00"')
        nxt('energy.txt="ENERGY: 0 Wh"')
        nxt('cost.txt="COST : 0.00 TL"')


# ─── Yardımcı Fonksiyonlar ────────────────────────────────────────────────────

def next_id() -> str:
    global msg_id
    i = msg_id
    msg_id += 1
    return str(i)


def iso_now() -> str:
    return datetime.now(TZ_UTC).isoformat().replace("+00:00", "Z")


# ─── OCPP Gönderme ───────────────────────────────────────────────────────────

async def send(ws, action: str, payload: dict) -> str:
    mid = next_id()
    msg = json.dumps([2, mid, action, payload])
    await ws.send(msg)
    log("SEND", f"[{action}] {payload}")
    return mid


async def send_result(ws, mid: str, payload: dict):
    msg = json.dumps([3, mid, payload])
    await ws.send(msg)
    log("SEND", f"[Response/{mid}] {payload}")


# ─── OCPP 1.6 Mesajları ──────────────────────────────────────────────────────

async def boot_notification(ws):
    await send(ws, "BootNotification", {
        "chargePointVendor":       VENDOR,
        "chargePointModel":        MODEL,
        "chargePointSerialNumber": SERIAL,
        "firmwareVersion":         FIRMWARE,
    })


async def heartbeat(ws):
    await send(ws, "Heartbeat", {})


async def status_notification(ws, connector_id: int, status: str, error_code: str = "NoError"):
    """
    CSMS'e StatusNotification gönderir ve Nextion'ı günceller.
    connector_id=0 → charge point seviyesi
    connector_id=1 → konnektör 1
    """
    await send(ws, "StatusNotification", {
        "connectorId": connector_id,
        "status":      status,
        "errorCode":   error_code,
        "timestamp":   iso_now(),
    })
    # Konnektör 1 için Nextion con objesini güncelle
    if connector_id == 1:
        nxt_set_status(status.upper())


async def authorize(ws, id_tag: str = DEFAULT_ID_TAG):
    await send(ws, "Authorize", {"idTag": id_tag})


async def start_transaction(ws, id_tag: str = DEFAULT_ID_TAG):
    """Şarj başlat: CSMS'e StartTransaction + konnektör 'Charging' durumuna geç."""
    global meter_wh, charging_active, charge_start_time, charge_percent
    global total_energy_wh, total_cost
    charge_start_time = time.time()
    charging_active   = True
    charge_percent    = 0
    total_energy_wh   = 0
    total_cost        = 0.0
    nxt_set_charge_percent(0)
    await send(ws, "StartTransaction", {
        "connectorId": 1,
        "idTag":       id_tag,
        "meterStart":  meter_wh,
        "timestamp":   iso_now(),
    })
    # Konnektör durumunu CSMS + Nextion'da CHARGING yap
    await status_notification(ws, 1, "Charging")


async def stop_transaction(ws):
    """Şarj durdur: CSMS'e StopTransaction + konnektör 'Available' durumuna geç."""
    global transaction_id, meter_wh, charging_active
    if transaction_id is None:
        log("WARN", "Aktif işlem yok!")
        return
    charging_active = False
    await send(ws, "StopTransaction", {
        "transactionId": transaction_id,
        "idTag":         DEFAULT_ID_TAG,
        "meterStop":     meter_wh,
        "timestamp":     iso_now(),
        "reason":        "Local",
    })
    # Status sayfasını dondur (son değerler kalır)
    nxt_update_status()
    log("INFO", f"Şarj durduruldu — Toplam: {total_energy_wh} Wh, {round(total_cost, 2)} TL")
    # Konnektör durumunu CSMS + Nextion'da AVAILABLE yap
    await status_notification(ws, 1, "Available")


async def meter_values(ws):
    """MeterValues gönder ve Nextion'ı güncelle."""
    global meter_wh, transaction_id, charge_percent, total_energy_wh, total_cost

    if not charging_active:
        log("WARN", "Şarj aktif değil — MeterValues gönderilmedi")
        return

    meter_wh        += METER_INCREMENT_WH
    charge_percent  = min(charge_percent + PERCENT_PER_STEP, 100)
    total_energy_wh += WH_PER_STEP
    total_cost      += TL_PER_500WH

    payload = {
        "connectorId": 1,
        "meterValue": [{
            "timestamp": iso_now(),
            "sampledValue": [
                {"value": str(meter_wh),        "measurand": "Energy.Active.Import.Register", "unit": "Wh"},
                {"value": str(DEFAULT_VOLTAGE),  "measurand": "Voltage",        "unit": "V"},
                {"value": str(DEFAULT_CURRENT),  "measurand": "Current.Import", "unit": "A"},
            ]
        }]
    }
    if transaction_id:
        payload["transactionId"] = transaction_id

    await send(ws, "MeterValues", payload)

    # Nextion güncelle
    nxt_set_charge_percent(charge_percent)
    nxt_update_status()
    log("INFO", f"Şarj: %{charge_percent} | Enerji: {total_energy_wh} Wh | Ücret: {total_cost:.2f} TL")


# ─── Periyodik Görevler ───────────────────────────────────────────────────────

async def heartbeat_loop(ws, interval: int):
    log("INFO", f"Heartbeat başladı — her {interval}s")
    while True:
        await asyncio.sleep(interval)
        try:
            await heartbeat(ws)
        except Exception:
            break


async def clock_loop():
    """Her saniye Nextion home sayfasındaki saati güncelle."""
    while True:
        nxt_set_time()
        await asyncio.sleep(1)


async def status_update_loop():
    """Şarj aktifken her saniye status sayfasını güncelle."""
    while True:
        if charging_active:
            nxt_update_status()
        await asyncio.sleep(1)


# ─── Gelen Mesaj İşleyici ─────────────────────────────────────────────────────

async def handle_message(ws, raw: str):
    global transaction_id, hb_interval, hb_task, is_connected

    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        log("ERR", f"JSON parse hatası: {raw}")
        return

    msg_type = msg[0]
    mid      = msg[1]

    if msg_type == 3:
        payload = msg[2]
        log("RECV", f"[Response/{mid}] {payload}")

        # BootNotification → heartbeat aralığı güncelle + konnektör durumu bildir
        if "interval" in payload and "status" in payload:
            status   = payload["status"]
            interval = payload.get("interval", hb_interval)
            log("INFO", f"BootNotification: status={status}, interval={interval}s")
            if status == "Accepted":
                hb_interval = interval
                if hb_task:
                    hb_task.cancel()
                hb_task = asyncio.create_task(heartbeat_loop(ws, hb_interval))

                # BootNotification kabul edildi:
                # → Charge Point (connector 0) + Konnektör 1 → AVAILABLE
                asyncio.create_task(status_notification(ws, 0, "Available"))
                asyncio.create_task(status_notification(ws, 1, "Available"))
                log("INFO", "Konnektör → AVAILABLE (CSMS + Nextion güncellendi)")

        # StartTransaction → transactionId kaydet
        if "transactionId" in payload:
            transaction_id = payload["transactionId"]
            log("INFO", f"Transaction ID={transaction_id}")

    elif msg_type == 2:
        action  = msg[2]
        payload = msg[3] if len(msg) > 3 else {}
        log("RECV", f"[{action}] ← Sunucu: {payload}")

        responses = {
            "GetConfiguration":       {"configurationKey": [], "unknownKey": []},
            "ChangeConfiguration":    {"status": "Accepted"},
            "Reset":                  {"status": "Accepted"},
            "RemoteStartTransaction": {"status": "Accepted"},
            "RemoteStopTransaction":  {"status": "Accepted"},
            "TriggerMessage":         {"status": "Accepted"},
            "UnlockConnector":        {"status": "Unlocked"},
            "ClearCache":             {"status": "Accepted"},
        }
        await send_result(ws, mid, responses.get(action, {}))

    elif msg_type == 4:
        log("ERR", f"CALLERROR [{mid}]: {msg[2]} — {msg[3]}")


# ─── Konsol Menüsü ────────────────────────────────────────────────────────────

def print_menu():
    print(f"""
{BOLD}{CYAN}┌──────────────────────────────────────────────┐{RESET}
{BOLD}{CYAN}│  OCPP 1.6J Simülatör · Nextion Entegrasyonu  │{RESET}
{BOLD}{CYAN}└──────────────────────────────────────────────┘{RESET}
  {BOLD}1{RESET}  BootNotification
  {BOLD}2{RESET}  Heartbeat (manual)
  {BOLD}3{RESET}  StatusNotification → Available
  {BOLD}4{RESET}  StatusNotification → Charging
  {BOLD}5{RESET}  Authorize ({DEFAULT_ID_TAG})
  {BOLD}6{RESET}  StartTransaction   [şarjı başlat]
  {BOLD}7{RESET}  MeterValues        [+500 Wh, +%5]
  {BOLD}8{RESET}  StopTransaction    [şarjı durdur]
  {BOLD}m{RESET}  Menüyü göster
  {BOLD}q{RESET}  Çıkış (konnektör → NOT CONNECTED)
""")


async def console_input(ws):
    loop = asyncio.get_event_loop()
    print_menu()
    while True:
        try:
            choice = await loop.run_in_executor(None, input, f"\n{BOLD}>{RESET} ")
            choice = choice.strip().lower()

            if choice == "1":
                await boot_notification(ws)
            elif choice == "2":
                await heartbeat(ws)
            elif choice == "3":
                await status_notification(ws, 1, "Available")
            elif choice == "4":
                await status_notification(ws, 1, "Charging")
            elif choice == "5":
                await authorize(ws)
            elif choice == "6":
                await start_transaction(ws)
            elif choice == "7":
                await meter_values(ws)
            elif choice == "8":
                await stop_transaction(ws)
            elif choice in ("q", "quit", "exit"):
                log("INFO", "Çıkılıyor — konnektör NOT CONNECTED yapılıyor...")
                # CSMS'e Unavailable bildir (bağlantı henüz açık)
                try:
                    await status_notification(ws, 1, "Unavailable")
                except Exception:
                    pass
                # Nextion'ı NOT CONNECTED yap (kırmızı, araç görseli gizli)
                nxt_set_status("NOT CONNECTED")
                sys.exit(0)
            elif choice == "m":
                print_menu()
            else:
                log("WARN", f"Bilinmeyen komut: '{choice}' — 'm' ile menüye bak")

        except (EOFError, KeyboardInterrupt):
            break


# ─── Alma Döngüsü ─────────────────────────────────────────────────────────────

async def recv_loop(ws):
    try:
        async for message in ws:
            await handle_message(ws, message)
    except websockets.exceptions.ConnectionClosed as e:
        log("WARN", f"Bağlantı kapandı: code={e.code}")


# ─── Ana Fonksiyon ────────────────────────────────────────────────────────────

async def main():
    global hb_task, is_connected, _nxt_queue

    print(f"\n{BOLD}OCPP 1.6J Simülatör · Nextion 3.5\" Entegrasyonu{RESET}")
    print(f"{DIM}Bağlanılıyor: {CSMS_URL}{RESET}\n")

    # ── Nextion yazma kuyruğu ve arka plan yazıcı görevi başlat ────────────
    # Event loop içinde oluşturulmalı → main() başında initialize edilir.
    _nxt_queue = asyncio.Queue(maxsize=64)
    asyncio.create_task(nxt_writer_loop())   # arka planda sonsuza kadar çalışır

    # Nextion portu aç
    nextion_open()
    # Seri hattın oturması + tampon temizliğinin etkili olması için bekleme.
    # Bu 300ms olmadan ilk komutlar hizasız gönderilip crash'e neden olabilir.
    await asyncio.sleep(0.3)

    # ── Başlangıç ekran durumu ──────────────────────────────────────────────
    # user_info sayfası: config'den okunan DEFAULT_ID_TAG, her başlatmada yazılır
    nxt_set_user_id(DEFAULT_ID_TAG)
    # Bağlantı henüz yok → NOT CONNECTED (kırmızı)
    nxt_set_status("NOT CONNECTED")
    nxt_set_charge_percent(0)
    nxt_set_time()

    def handle_sigint(*_):
        log("INFO", "Ctrl+C — bağlantı kesiliyor...")
        nxt_set_status("NOT CONNECTED")
        sys.exit(0)
    signal.signal(signal.SIGINT, handle_sigint)

    try:
        _credentials = base64.b64encode(
            f"{BASIC_AUTH_USER}:{BASIC_AUTH_PASSWORD}".encode()
        ).decode()

        async with websockets.connect(
            CSMS_URL,
            subprotocols=[SUBPROTOCOL],
            ping_interval=PING_INTERVAL,
            extra_headers={"Authorization": f"Basic {_credentials}"},
        ) as ws:
            is_connected = True
            log("INFO", f"Bağlandı ✓  ({CSMS_URL})")

            # WebSocket bağlandı → BootNotification gönder.
            # BootNotification yanıtı (Accepted) gelince handle_message içinde
            # StatusNotification(Available) otomatik gönderilir ve Nextion
            # AVAILABLE (cyan, 2047) durumuna geçer.
            await boot_notification(ws)

            # Paralel görevler
            recv_task    = asyncio.create_task(recv_loop(ws))
            input_task   = asyncio.create_task(console_input(ws))
            clock_task   = asyncio.create_task(clock_loop())
            status_task  = asyncio.create_task(status_update_loop())
            nextion_task = asyncio.create_task(nextion_read_loop())

            await asyncio.gather(recv_task, input_task, clock_task, status_task, nextion_task)

    except OSError as e:
        log("ERR", f"Bağlantı başarısız: {e}")
        log("INFO", "CSMS çalışıyor mu? URL doğru mu?")
        nxt_set_status("NOT CONNECTED")
    except Exception as e:
        log("ERR", f"Beklenmeyen hata: {e}")
        nxt_set_status("NOT CONNECTED")


# ─── Giriş Noktası ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # 1. NFC kart doğrulaması — geçerli kart okutulana kadar bloke eder
    wait_for_nfc_auth()
    # 2. Doğrulama başarılı → OCPP simülatörünü başlat
    asyncio.run(main())
