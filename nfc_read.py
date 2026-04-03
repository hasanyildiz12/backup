#!/usr/bin/env python3
"""
nfc_read.py — PN532 I2C NFC Kart Okuyucu
=========================================
Raspberry Pi Zero 2W · I2C · smbus2
PN532_ADDR = 0x24

Kütüphane olarak import edilebilir:
    from nfc_read import init_pn532, read_uid

Bağımsız test için:
    python nfc_read.py
"""

import smbus2
import time

bus = smbus2.SMBus(1)
PN532_ADDR = 0x24


def pn532_write(data):
    try:
        msg = smbus2.i2c_msg.write(PN532_ADDR, data)
        bus.i2c_rdwr(msg)
    except OSError:
        time.sleep(0.05)
        try:
            msg = smbus2.i2c_msg.write(PN532_ADDR, data)
            bus.i2c_rdwr(msg)
        except Exception:
            pass


def pn532_read(length):
    for _ in range(15):  # Hazır olana kadar daha uzun bekle
        try:
            msg = smbus2.i2c_msg.read(PN532_ADDR, length + 1)
            bus.i2c_rdwr(msg)
            resp = list(msg)
            if resp[0] == 0x01:  # 0x01: Modül cevap vermeye hazır!
                return resp[1:]
        except OSError:
            pass
        time.sleep(0.05)
    return None


def init_pn532():
    """PN532 modülünü başlat (SAMConfiguration)."""
    print("Modül ayarlanıyor (SAMConfiguration)...")
    pn532_write([0x00, 0x00, 0xFF, 0x03, 0xFD, 0xD4, 0x14, 0x01, 0x17, 0x00])
    time.sleep(0.05)

    # 1. ACK paketini okuyup hattı temizle
    pn532_read(6)
    time.sleep(0.05)

    # 2. SAMConfig Yanıtını okuyup buffer'ı (tamponu) tamamen boşalt
    pn532_read(8)


def read_uid():
    """
    Bir NFC kartının UID'sini okur.
    Kart bulunursa bytes nesnesi döner, bulunmazsa None döner.
    """
    # InListPassiveTarget komutu (Kart arama)
    pn532_write([0x00, 0x00, 0xFF, 0x04, 0xFC, 0xD4, 0x4A, 0x01, 0x00, 0xE1, 0x00])
    time.sleep(0.05)

    # 1. ÖNCE "Komutu Aldım" (ACK) paketini oku
    ack = pn532_read(6)
    if not ack or len(ack) < 6:
        return None

    # Kartın manyetik alana girip okunması için ufak bir bekleme
    time.sleep(0.1)

    # 2. ŞİMDİ asıl kart verisini (UID) oku
    resp = pn532_read(22)

    if resp and len(resp) >= 13:
        # Doğru yanıt formatı: D5 4B (Hedef bulundu)
        if resp[5] == 0xD5 and resp[6] == 0x4B and resp[7] > 0:
            uid_len = resp[12]
            uid = resp[13:13 + uid_len]
            return bytes(uid)

    return None


# ─── Bağımsız Test Modu ───────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Başlatılıyor...")
    init_pn532()

    print("Kartı okutun...")
    while True:
        try:
            uid = read_uid()
            if uid:
                print(">>> KART BULUNDU! ID:", uid.hex().upper())
                time.sleep(1)  # Aynı kartı saniyede 1 kere okumak için bekleme
            time.sleep(0.1)
        except KeyboardInterrupt:
            print("\nÇıkış yapılıyor...")
            break