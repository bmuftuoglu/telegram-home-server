# Telegram Home Server

Kişisel ev otomasyonu için merkezi bir Telegram botu. Docker Compose ile çalışır, yeni servisler eklenerek genişletilebilir.

```
ASKİ sitesi
    ↓ (her 10 dk)
aski-water-watch  ──POST /notify──▶  telegram-bot-gateway  ──▶  Telegram
                                              ▲
                               kullanıcı /aski_durum komutu
```

## Servisler

- **telegram-bot-gateway** — Telegram botunu yönetir, komutları karşılar, kullanıcı doğrulaması yapar, iç servislerden gelen bildirimleri Telegram'a iletir.
- **aski-water-watch** — ASKİ su kesintisi sayfasını periyodik olarak kontrol eder, kesinti başladığında veya sona erdiğinde gateway üzerinden bildirim gönderir. Kaynak: [aski-telegram-bot](https://github.com/bmuftuoglu/aski-telegram-bot)

## Gereksinimler

- [Docker](https://docs.docker.com/get-docker/) ve [Docker Compose](https://docs.docker.com/compose/install/)
- Bir Telegram hesabı

## Kurulum

### 1. Telegram botu oluştur

1. Telegram'da **@BotFather**'ı aç
2. `/newbot` komutunu gönder, bir isim ve kullanıcı adı belirle
3. Verilen token'ı kopyala

### 2. Telegram kullanıcı ID'ni öğren

Telegram'da **@userinfobot**'a mesaj at, sana user ID'ni söyler.

### 3. Güvenli bir iç token üret

Servisler arası iletişimi şifrelemek için rastgele bir token gerekir:

```bash
openssl rand -hex 32
```

### 4. Yapılandırma dosyasını oluştur

```bash
cp .env.example .env
```

`.env` dosyasını açıp şu değerleri doldur:

```env
TELEGRAM_BOT_TOKEN=BotFather_dan_aldigin_token
TELEGRAM_ALLOWED_USER_IDS=telegram_user_id
TELEGRAM_DEFAULT_CHAT_ID=telegram_user_id
INTERNAL_API_TOKEN=openssl_ile_uretilen_token

ASKI_TARGET_DISTRICT=ÇANKAYA
ASKI_TARGET_NEIGHBORHOOD=Mahalle Adı
```

`TELEGRAM_ALLOWED_USER_IDS` birden fazla kullanıcı için virgülle ayrılabilir: `123,456`.

### 5. Başlat

```bash
docker compose up --build -d
```

## Telegram Komutları

| Komut | Açıklama |
| --- | --- |
| `/start` | Komut listesini göster |
| `/help` | Komut listesini göster |
| `/services` | Kayıtlı servisleri listele |
| `/aski_durum` | Son ASKİ kesinti durumunu göster |
| `/aski_kontrol` | Manuel ASKİ kontrolü başlat |

Bot long polling kullandığı için sunucunun dışarıya açık bir portu olması gerekmez.

## Ortam Değişkenleri

| Değişken | Varsayılan | Açıklama |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | zorunlu | BotFather'dan alınan token. |
| `TELEGRAM_ALLOWED_USER_IDS` | zorunlu | Botu kullanabilecek user ID'leri (virgülle). |
| `TELEGRAM_DEFAULT_CHAT_ID` | zorunlu | Bildirimlerin gönderileceği chat ID. |
| `INTERNAL_API_TOKEN` | zorunlu | Servisler arası iletişim için gizli token. |
| `ASKI_TARGET_DISTRICT` | zorunlu | Takip edilecek ilçe (büyük harf, örn. `ÇANKAYA`). |
| `ASKI_TARGET_NEIGHBORHOOD` | zorunlu | Takip edilecek mahalle adı. |
| `ASKI_URL` | ASKİ kesinti sayfası | Değiştirme gerekmez. |
| `CHECK_INTERVAL_SECONDS` | `600` | Kontrol aralığı (saniye). |
| `ASKI_NOTIFY_EVERY_CHECK` | `false` | `true` yapılırsa her kontrolde bildirim gönderir. |

## Yeni Servis Eklemek

Her yeni servis ayrı bir container olarak çalışır. Dil fark etmez (Python, Node.js, Go...).

1. `services/` altına yeni servis dizinini oluştur
2. Servis, bildirimleri gateway'in `/notify` endpoint'ine POST ile gönderir:
   ```json
   { "text": "Bildirim metni" }
   ```
   Header: `Authorization: Bearer $INTERNAL_API_TOKEN`
3. `docker-compose.yml`'e yeni servisi ekle
4. `telegram-bot-gateway/src/app.py`'ye komut handler'ı ekle

## Güvenlik

- `.env` dosyasını asla Git'e commit etme.
- `INTERNAL_API_TOKEN` tahmin edilemez olmalı, `openssl rand -hex 32` ile üret.
- Gateway yalnızca `TELEGRAM_ALLOWED_USER_IDS` listesindeki kullanıcıların komutlarını işler.
