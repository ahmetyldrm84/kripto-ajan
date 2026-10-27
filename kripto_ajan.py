"""
Kripto Analiz Ajanı v2 · Gate.io USDT perpetual tarayıcı + pozisyon takipçisi + Telegram

Ne yapar?
- TARAMA: 15 dakikalık Donchian(20) kırılımı + 1 saatlik EMA50/200 trend filtresi.
  Güven skoru kurallarla hesaplanır; yalnızca MIN_GUVEN ve üstü sinyal kartı gönderilir.
- TAKİP: Girdiğin işlemi Telegram'dan /ekle ile bildirirsin, bot her 5 dakikada bir
  fiyatı kontrol eder: kâr kilitleme önerir, stop'a yaklaşma ve trend dönüşü uyarısı verir.
- ÖZET: Sabah 09:00 ve akşam 21:00 (TR) kısa piyasa özeti.

Ne yapmaz?
- Emir girmez, API anahtarı istemez. Gerçek stop-loss Gate.io'da durmalı.
- Stop genişletmeyi kabul etmez.

Çalıştırma:
  python kripto_ajan.py           -> tek tur (GitHub Actions bunu 5 dakikada bir çalıştırır)
  python kripto_ajan.py --dongu   -> bilgisayarında sürekli çalışır, komutlara anında cevap verir
  python kripto_ajan.py --test    -> Telegram'a göndermez, ekrana yazar
  python kripto_ajan.py --ozet    -> özeti hemen üretir

Telegram komutları:
  /ekle XRP LONG 1.3240 1.3150 1.3420 300   (coin yön giriş stop tp [değer USDT])
  /sl XRP 1.3240      stop'u günceller (yalnızca kâr yönünde)
  /tp XRP 1.3500      take-profit'i günceller
  /kapat XRP          pozisyonu takipten çıkarır
  /liste              açık pozisyonların anlık durumu
  /yardim             komut listesi
"""

import os
import sys
import json
import html
import time
import datetime as dt

import requests

# ---------------------------------------------------------------- AYARLAR
IZLEME = [k.strip() for k in os.getenv("IZLEME", "BTC_USDT,ETH_USDT,XRP_USDT,SOL_USDT").split(",") if k.strip()]
BAKIYE = float(os.getenv("BAKIYE") or 45)            # Gate.io bakiyen
DURDURMA = float(os.getenv("DURDURMA") or 20)        # kill switch
MIN_GUVEN = int(os.getenv("MIN_GUVEN") or 7)         # bunun altı sinyal gönderilmez
MAKS_RISK_YUZDE = float(os.getenv("MAKS_RISK") or 5) # stop'ta bakiyenin en fazla %5'i
OZET_SAATLERI_UTC = (6, 18)                           # TR saatiyle 09:00 ve 21:00
DONGU_SANIYE = 300                                    # --dongu modunda tur aralığı

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
DURUM_DOSYA = os.getenv("DURUM_DOSYA", "durum.json")
GATE = "https://api.gateio.ws/api/v4"
TEST = "--test" in sys.argv


def esc(x):
    return html.escape(str(x), quote=False)


# ---------------------------------------------------------------- DURUM (hafıza)
def durum_oku():
    try:
        with open(DURUM_DOSYA, encoding="utf-8") as f:
            d = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        d = {}
    d.setdefault("offset", 0)
    d.setdefault("pozisyonlar", {})
    d.setdefault("taranan", {})
    d.setdefault("son_ozet", "")
    return d


def durum_yaz(d):
    with open(DURUM_DOSYA, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------- VERİ
def _get(yol, **params):
    r = requests.get(GATE + yol, params=params, timeout=15)
    r.raise_for_status()
    return r.json()


def mumlar(kontrat, aralik, adet):
    veri = _get("/futures/usdt/candlesticks", contract=kontrat, interval=aralik, limit=adet)
    return [
        {"t": int(d["t"]), "o": float(d["o"]), "h": float(d["h"]),
         "l": float(d["l"]), "c": float(d["c"]), "v": float(d.get("v") or 0)}
        for d in veri
    ]


def ticker(kontrat):
    d = _get("/futures/usdt/tickers", contract=kontrat)[0]
    return {
        "son": float(d["last"]),
        "degisim": float(d.get("change_percentage") or 0),
        "funding": float(d.get("funding_rate") or 0),
    }


def defter_dengesi(kontrat):
    """0-1 arası: 1'e yakınsa alış ağır, 0'a yakınsa satış ağır."""
    d = _get("/futures/usdt/order_book", contract=kontrat, limit=20)
    alis = sum(float(x["s"]) for x in d.get("bids", []))
    satis = sum(float(x["s"]) for x in d.get("asks", []))
    return alis / (alis + satis) if alis + satis else 0.5


def fear_greed():
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1", timeout=15)
        d = r.json()["data"][0]
        return int(d["value"]), d["value_classification"]
    except Exception:
        return None, "veri yok"


# ---------------------------------------------------------------- GÖSTERGELER
def ema(seri, n):
    k = 2 / (n + 1)
    e = seri[0]
    for x in seri[1:]:
        e = x * k + e * (1 - k)
    return e


def atr(m, n=14):
    tr = [max(b["h"] - b["l"], abs(b["h"] - a["c"]), abs(b["l"] - a["c"]))
          for a, b in zip(m[:-1], m[1:])]
    return sum(tr[-n:]) / n


def trend_1s(kontrat):
    k = [b["c"] for b in mumlar(kontrat, "1h", 250)[:-1]]
    return "yukari" if ema(k, 50) > ema(k, 200) else "asagi"


def fiyat_yaz(x):
    if x >= 1000:
        return f"{x:,.1f}"
    if x >= 10:
        return f"{x:,.2f}"
    return f"{x:.4f}"


def sayi(s):
    """'1,3240' ve '1.3240' ikisini de kabul eder. Binlik ayırıcı kullanma."""
    s = s.strip()
    if "," in s and "." in s:
        s = s.replace(".", "")
    return float(s.replace(",", "."))


def kontrat_adi(s):
    s = s.upper().replace("/", "").replace("_", "").replace("-", "")
    if s.endswith("USDT"):
        s = s[:-4]
    return f"{s}_USDT"


# ---------------------------------------------------------------- TARAMA
def analiz_et(kontrat, fng):
    m15 = mumlar(kontrat, "15m", 120)[:-1]   # son mum kapanmadı, at
    h1 = mumlar(kontrat, "1h", 250)[:-1]
    son = m15[-1]
    pencere = m15[-21:-1]
    ust = max(b["h"] for b in pencere)
    alt = min(b["l"] for b in pencere)

    if son["c"] > ust:
        yon = "LONG"
    elif son["c"] < alt:
        yon = "SHORT"
    else:
        return None  # kırılım yok, sessiz kal

    kapanislar = [b["c"] for b in h1]
    e50, e200 = ema(kapanislar, 50), ema(kapanislar, 200)
    ort_hacim = sum(b["v"] for b in pencere) / len(pencere) or 1
    hacim_kat = son["v"] / ort_hacim
    t = ticker(kontrat)
    denge = defter_dengesi(kontrat)

    puan, notlar = 5, []
    if yon == "LONG":
        uyum = e50 > e200 and son["c"] > e50
        ters = e50 < e200 and son["c"] < e50
    else:
        uyum = e50 < e200 and son["c"] < e50
        ters = e50 > e200 and son["c"] > e50
    if uyum:
        puan += 2; notlar.append("1s trendi kırılımla aynı yönde")
    elif ters:
        puan -= 2; notlar.append("1s trendine karşı kırılım")
    if hacim_kat >= 2.5:
        puan += 2; notlar.append(f"hacim ortalamanın {hacim_kat:.1f} katı")
    elif hacim_kat >= 1.5:
        puan += 1; notlar.append(f"hacim ortalamanın {hacim_kat:.1f} katı")
    elif hacim_kat < 1.0:
        puan -= 1; notlar.append("hacimsiz kırılım")
    f = t["funding"]
    if (yon == "LONG" and f > 0.0005) or (yon == "SHORT" and f < -0.0005):
        puan -= 1; notlar.append(f"funding {f*100:+.4f}% ile taraf kalabalık")
    elif (yon == "LONG" and f < 0) or (yon == "SHORT" and f > 0.0003):
        puan += 1; notlar.append(f"funding {f*100:+.4f}% karşı tarafı sıkıştırıyor")
    lehte = denge if yon == "LONG" else 1 - denge
    if lehte >= 0.6:
        puan += 1; notlar.append(f"defter lehte (%{lehte*100:.0f})")
    elif lehte <= 0.4:
        puan -= 1; notlar.append(f"defter aleyhte (%{lehte*100:.0f})")
    if fng is not None:
        if (yon == "LONG" and fng >= 80) or (yon == "SHORT" and fng <= 20):
            puan -= 1; notlar.append(f"F&G {fng}, duygu aşırı")
    puan = max(1, min(10, puan))

    a = atr(m15)
    giris = son["c"]
    stop = giris - 1.5 * a if yon == "LONG" else giris + 1.5 * a
    tp = giris + 2 * (giris - stop)
    stop_yuzde = abs(giris - stop) / giris

    if puan >= 8:
        marj, kaldirac = BAKIYE * 0.45, 7
    else:
        marj, kaldirac = BAKIYE * 0.22, 4
    maks_kayip = BAKIYE * MAKS_RISK_YUZDE / 100
    while kaldirac > 1 and marj * kaldirac * stop_yuzde > maks_kayip:
        kaldirac -= 1
    if marj * kaldirac * stop_yuzde > maks_kayip:
        marj = maks_kayip / stop_yuzde
    deger = marj * kaldirac

    return {
        "kontrat": kontrat, "yon": yon, "puan": puan, "notlar": notlar,
        "giris": giris, "stop": stop, "tp": tp, "marj": marj, "mum": son["t"],
        "kaldirac": kaldirac, "deger": deger, "kayip": deger * stop_yuzde,
        "kazanc": deger * stop_yuzde * 2,
    }


def kart(s):
    ad = s["kontrat"].replace("_", "")
    coin = s["kontrat"].replace("_USDT", "")
    g = "\n".join(f"• {n}" for n in s["notlar"])
    metin = (
        f"PARİTE / YÖN: {ad} · {s['yon']}\n"
        f"KALDIRAÇ: {s['kaldirac']}x · ISOLATED\n"
        f"MARJ: {s['marj']:.2f} USDT (Value: {s['deger']:.0f} USDT)\n"
        f"GİRİŞ: {fiyat_yaz(s['giris'])} civarı (kovalama)\n"
        f"STOP-LOSS: {fiyat_yaz(s['stop'])}\n"
        f"TAKE-PROFIT: {fiyat_yaz(s['tp'])}\n"
        f"GÜVEN: {s['puan']}/10\n"
        f"RİSK: stop'ta ≈ -{s['kayip']:.2f} / TP'de ≈ +{s['kazanc']:.2f} USDT"
    )
    ekle = (f"/ekle {coin} {s['yon']} {fiyat_yaz(s['giris']).replace(',', '')} "
            f"{fiyat_yaz(s['stop']).replace(',', '')} {fiyat_yaz(s['tp']).replace(',', '')} "
            f"{s['deger']:.0f}")
    return (f"🎯 <b>SİNYAL · {ad} {s['yon']}</b>\n<pre>{esc(metin)}</pre>\n"
            f"<b>Gerekçe</b>\n{esc(g)}\n\n"
            f"Girersen takibe almak için (giriş fiyatını kendi dolumunla değiştir):\n"
            f"<code>{esc(ekle)}</code>\n\n"
            f"<i>Bot emir girmez. Stop genişletme yok, zarara ekleme yok.</i>")


def ozet(fng, fng_ad):
    satirlar = []
    for k in IZLEME:
        try:
            t = ticker(k)
            trend = "↗ yukarı" if trend_1s(k) == "yukari" else "↘ aşağı"
            satirlar.append(f"{k.replace('_USDT',''):<4} {fiyat_yaz(t['son']):>10} "
                            f"{t['degisim']:+.2f}%  f:{t['funding']*100:+.4f}%  {trend}")
        except Exception as e:
            satirlar.append(f"{k}: veri alınamadı ({e.__class__.__name__})")
    bas = f"📊 <b>Piyasa özeti</b> · F&G: {fng if fng is not None else '-'} ({esc(fng_ad)})"
    return bas + "\n<pre>" + esc("\n".join(satirlar)) + "</pre>"


# ---------------------------------------------------------------- POZİSYON TAKİBİ
YARDIM = (
    "<b>Komutlar</b>\n"
    "<code>/ekle XRP LONG 1.3240 1.3150 1.3420 300</code>\n"
    "  coin · yön · giriş · stop · tp · değer(USDT, isteğe bağlı)\n"
    "<code>/sl XRP 1.3240</code> stop'u güncelle (yalnızca kâr yönünde)\n"
    "<code>/tp XRP 1.3500</code> take-profit'i güncelle\n"
    "<code>/kapat XRP</code> takipten çıkar\n"
    "<code>/liste</code> açık pozisyonların durumu\n\n"
    "Önce değişikliği Gate.io'da yap, sonra bota bildir."
)


def r_hesapla(p, fiyat):
    y = 1 if p["yon"] == "LONG" else -1
    r_birim = abs(p["giris"] - p["sl_ilk"]) or 1e-12
    return (fiyat - p["giris"]) * y / r_birim


def pnl_yaz(p, fiyat):
    if not p.get("deger"):
        return ""
    y = 1 if p["yon"] == "LONG" else -1
    return f" · ≈ {p['deger'] * (fiyat - p['giris']) / p['giris'] * y:+.2f} USDT"


def komut_isle(metin, durum):
    parca = metin.split()
    if not parca:
        return
    komut = parca[0].lower().split("@")[0]
    poz = durum["pozisyonlar"]
    try:
        if komut in ("/start", "/yardim", "/help"):
            gonder(YARDIM)

        elif komut == "/ekle":
            if len(parca) < 6:
                gonder("Eksik bilgi. Örnek:\n<code>/ekle XRP LONG 1.3240 1.3150 1.3420 300</code>")
                return
            k = kontrat_adi(parca[1])
            yon = parca[2].upper()
            giris, sl, tp = sayi(parca[3]), sayi(parca[4]), sayi(parca[5])
            deger = sayi(parca[6]) if len(parca) > 6 else None
            if yon not in ("LONG", "SHORT"):
                gonder("Yön LONG ya da SHORT olmalı.")
                return
            if (yon == "LONG" and not sl < giris < tp) or (yon == "SHORT" and not tp < giris < sl):
                gonder("Seviyeler tutarsız. LONG için stop < giriş < tp, SHORT için tp < giriş < stop olmalı.")
                return
            ticker(k)  # kontrat gerçekten var mı?
            poz[k] = {"yon": yon, "giris": giris, "sl": sl, "sl_ilk": sl, "tp": tp,
                      "deger": deger, "acilis": dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes"),
                      "uyarilar": []}
            risk = f" · stop'ta ≈ -{deger * abs(giris - sl) / giris:.2f} USDT" if deger else ""
            gonder(f"✅ Takibe alındı: <b>{k.replace('_', '')} {yon}</b>\n"
                   f"Giriş {fiyat_yaz(giris)} · SL {fiyat_yaz(sl)} · TP {fiyat_yaz(tp)}{risk}\n"
                   f"Stop'un Gate.io'da da girili olduğundan emin ol.")

        elif komut == "/sl":
            k = kontrat_adi(parca[1]); yeni = sayi(parca[2])
            p = poz.get(k)
            if not p:
                gonder(f"{k} takipte değil.")
                return
            daha_sıkı = yeni > p["sl"] if p["yon"] == "LONG" else yeni < p["sl"]
            if not daha_sıkı:
                gonder("⛔ <b>Stop genişletme yasak.</b> Stop yalnızca kâr yönünde taşınabilir.\n"
                       "Zararı ertelemenin en pahalı yolu budur, mevcut stop kalsın.")
                return
            if (p["yon"] == "LONG" and yeni >= p["tp"]) or (p["yon"] == "SHORT" and yeni <= p["tp"]):
                gonder("Yeni stop take-profit'in ötesinde olamaz.")
                return
            p["sl"] = yeni
            gonder(f"🔒 {k.replace('_', '')} stop güncellendi: {fiyat_yaz(yeni)}")

        elif komut == "/tp":
            k = kontrat_adi(parca[1]); yeni = sayi(parca[2])
            p = poz.get(k)
            if not p:
                gonder(f"{k} takipte değil.")
                return
            if (p["yon"] == "LONG" and yeni <= p["sl"]) or (p["yon"] == "SHORT" and yeni >= p["sl"]):
                gonder("Take-profit stop'un yanlış tarafında olamaz.")
                return
            p["tp"] = yeni
            gonder(f"🎯 {k.replace('_', '')} TP güncellendi: {fiyat_yaz(yeni)}")

        elif komut == "/kapat":
            k = kontrat_adi(parca[1])
            if poz.pop(k, None):
                gonder(f"📕 {k.replace('_', '')} takipten çıkarıldı.")
            else:
                gonder(f"{k} takipte değil.")

        elif komut == "/liste":
            if not poz:
                gonder("Takipte pozisyon yok.")
                return
            satir = []
            for k, p in poz.items():
                fiyat = ticker(k)["son"]
                satir.append(f"{k.replace('_USDT',''):<4} {p['yon']:<5} giriş {fiyat_yaz(p['giris'])} "
                             f"şimdi {fiyat_yaz(fiyat)}\n     SL {fiyat_yaz(p['sl'])} TP {fiyat_yaz(p['tp'])} "
                             f"· {r_hesapla(p, fiyat):+.2f}R{pnl_yaz(p, fiyat)}")
            gonder("📋 <b>Açık pozisyonlar</b>\n<pre>" + esc("\n".join(satir)) + "</pre>")

        elif komut.startswith("/"):
            gonder("Bu komutu tanımıyorum. /yardim yaz.")
    except (ValueError, IndexError):
        gonder("Komutu okuyamadım. Sayılarda binlik ayırıcı kullanma. /yardim yaz.")
    except requests.RequestException:
        gonder("Gate.io'ya ulaşamadım ya da bu kontrat yok, coin adını kontrol et.")


def pozisyon_takip(durum):
    poz = durum["pozisyonlar"]
    for k in list(poz):
        p = poz[k]
        try:
            fiyat = ticker(k)["son"]
        except Exception as e:
            print(f"{k} takip verisi alınamadı: {e}")
            continue
        ad = k.replace("_", "")
        y = 1 if p["yon"] == "LONG" else -1
        r = round(r_hesapla(p, fiyat), 6)  # ondalık yuvarlama hatasına karşı
        uy = p.setdefault("uyarilar", [])

        if (fiyat - p["sl"]) * y <= 0:
            sonuc = "girişte (başa baş)" if abs(p["sl"] - p["giris"]) / p["giris"] < 0.0005 else \
                    ("kârda" if (p["sl"] - p["giris"]) * y > 0 else "zararda")
            gonder(f"🛑 <b>{ad}</b> stop seviyesi ({fiyat_yaz(p['sl'])}) geçildi, pozisyon {sonuc} "
                   f"kapanmış olmalı. Gate.io'da kontrol et.\nPlan işledi, stop'a sadık kaldın. Takipten çıkarıldı.")
            del poz[k]
            continue
        if (fiyat - p["tp"]) * y >= 0:
            gonder(f"🏁 <b>{ad}</b> take-profit ({fiyat_yaz(p['tp'])}) geldi! "
                   f"Gate.io'da kapandığını kontrol et. Takipten çıkarıldı.")
            del poz[k]
            continue

        # Kâr kilitleme: yalnızca stop'u kâr yönünde taşımayı önerir
        if r >= 1.5 and "iz" not in uy:
            yeni = p["giris"] + 0.5 * y * abs(p["giris"] - p["sl_ilk"])
            if (yeni - p["sl"]) * y > 0:
                gonder(f"💰 <b>{ad}</b> +{r:.1f}R kârda ({fiyat_yaz(fiyat)}{pnl_yaz(p, fiyat)}).\n"
                       f"Kârın bir kısmını kilitle: stop'u <b>{fiyat_yaz(yeni)}</b> seviyesine çek.\n"
                       f"Gate.io'da yaptıktan sonra: <code>/sl {k.replace('_USDT', '')} {fiyat_yaz(yeni).replace(',', '')}</code>")
            uy.extend(["iz", "be"])
        elif r >= 1 and "be" not in uy:
            if (p["giris"] - p["sl"]) * y > 0:
                gonder(f"🔐 <b>{ad}</b> +1R kârda ({fiyat_yaz(fiyat)}{pnl_yaz(p, fiyat)}).\n"
                       f"Stop'u girişe (<b>{fiyat_yaz(p['giris'])}</b>) çek, artık bu işlemden zarar edemezsin.\n"
                       f"Gate.io'da yaptıktan sonra: <code>/sl {k.replace('_USDT', '')} {fiyat_yaz(p['giris']).replace(',', '')}</code>")
            uy.append("be")

        # Stop'a yaklaşma uyarısı (bir kez)
        mesafe_ilk = abs(p["giris"] - p["sl_ilk"])
        if abs(fiyat - p["sl"]) <= 0.3 * mesafe_ilk and r < 0 and "yakin_sl" not in uy:
            gonder(f"⚠️ <b>{ad}</b> stop'a yaklaşıyor: fiyat {fiyat_yaz(fiyat)}, stop {fiyat_yaz(p['sl'])}"
                   f"{pnl_yaz(p, fiyat)}.\nStop'u genişletme. İstersen erken çık, istersen stop'un çalışmasına izin ver.")
            uy.append("yakin_sl")

        # Trend aleyhine döndü mü? (zarardayken, bir kez)
        if r < -0.3 and "trend" not in uy:
            try:
                tr = trend_1s(k)
                if (p["yon"] == "LONG" and tr == "asagi") or (p["yon"] == "SHORT" and tr == "yukari"):
                    gonder(f"🔄 <b>{ad}</b> zararda ({r:+.2f}R{pnl_yaz(p, fiyat)}) ve 1 saatlik trend "
                           f"pozisyonun aleyhine döndü.\nErken çıkıp kaybı küçültmeyi düşün. "
                           f"Stop'u beklemek zorunda değilsin.")
                    uy.append("trend")
            except Exception:
                pass


# ---------------------------------------------------------------- TELEGRAM
def gonder(metin):
    if TEST or not TELEGRAM_TOKEN:
        temiz = metin
        for e in ("<b>", "</b>", "<pre>", "</pre>", "<i>", "</i>", "<code>", "</code>"):
            temiz = temiz.replace(e, "")
        print(html.unescape(temiz))
        print("-" * 50)
        return
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      json={"chat_id": TELEGRAM_CHAT_ID, "text": metin,
                            "parse_mode": "HTML", "disable_web_page_preview": True},
                      timeout=15).raise_for_status()
    except requests.RequestException as e:
        print(f"Telegram gönderim hatası: {e}")


def telegram_mesajlari(durum, bekle=0):
    """Yalnızca senin sohbetinden (TELEGRAM_CHAT_ID) gelen mesajları döndürür."""
    if TEST or not TELEGRAM_TOKEN:
        return []
    try:
        r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                         params={"offset": durum["offset"], "timeout": bekle},
                         timeout=bekle + 15)
        r.raise_for_status()
    except requests.RequestException as e:
        print(f"Telegram okuma hatası: {e}")
        return []
    mesajlar = []
    for u in r.json().get("result", []):
        durum["offset"] = u["update_id"] + 1
        m = u.get("message") or {}
        if str(m.get("chat", {}).get("id")) == str(TELEGRAM_CHAT_ID) and m.get("text"):
            mesajlar.append(m["text"].strip())
    return mesajlar


# ---------------------------------------------------------------- ANA AKIŞ
def tur(durum):
    for m in telegram_mesajlari(durum):
        komut_isle(m, durum)

    pozisyon_takip(durum)

    if BAKIYE < DURDURMA:
        if durum.get("kill") != "gonderildi":
            gonder(f"⛔ Kill switch: bakiye {BAKIYE} < {DURDURMA}. Yeni sinyal yok, sadece takip.")
            durum["kill"] = "gonderildi"
        return

    fng, fng_ad = fear_greed()
    simdi = dt.datetime.now(dt.timezone.utc)
    ozet_anahtar = simdi.strftime("%Y-%m-%d %H")
    if "--ozet" in sys.argv or (simdi.hour in OZET_SAATLERI_UTC and durum["son_ozet"] != ozet_anahtar):
        gonder(ozet(fng, fng_ad))
        durum["son_ozet"] = ozet_anahtar

    for k in IZLEME:
        if k in durum["pozisyonlar"]:
            continue  # açık pozisyon varken aynı coine yeni sinyal yok
        try:
            s = analiz_et(k, fng)
        except Exception as e:
            print(f"{k} analiz hatası: {e}")
            continue
        if s is None:
            print(f"{k}: kırılım yok")
            continue
        if durum["taranan"].get(k) == s["mum"]:
            continue  # bu mumun sinyali zaten değerlendirildi
        durum["taranan"][k] = s["mum"]
        if s["puan"] < MIN_GUVEN:
            print(f"{k}: {s['yon']} kırılımı var ama güven {s['puan']}/10, gönderilmedi")
        else:
            gonder(kart(s))


def main():
    durum = durum_oku()
    if "--dongu" not in sys.argv:
        tur(durum)
        durum_yaz(durum)
        return

    print("Ajan çalışıyor. Durdurmak için Ctrl+C.")
    while True:
        tur(durum)
        durum_yaz(durum)
        bitis = time.time() + DONGU_SANIYE
        while time.time() < bitis:  # aradaki sürede komutlara anında cevap ver
            for m in telegram_mesajlari(durum, bekle=min(30, max(1, int(bitis - time.time())))):
                komut_isle(m, durum)
                durum_yaz(durum)
            if TEST or not TELEGRAM_TOKEN:
                time.sleep(max(0, bitis - time.time()))


if __name__ == "__main__":
    main()
