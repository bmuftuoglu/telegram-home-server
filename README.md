# Telegram Home Server

Kişisel ev otomasyonu için merkezi bir Telegram botu. Harici servislerden gelen bildirimleri Telegram'a iletir ve Telegram komutlarını iç servislere yönlendirir.

```
aski-water-watch  ──POST /notify──▶  telegram-bot-gateway  ──▶  Telegram
                                              ▲
                               kullanıcı /aski_durum komutu
```

Servisler birbirinden bağımsız repolar olarak çalışır ve `homebot` adlı paylaşılan bir Docker network üzerinden haberleşir.

## Servisler

- **telegram-bot-gateway** — Telegram botunu yönetir, komutları karşılar, kullanıcı doğrulaması yapar, iç servislerden gelen bildirimleri Telegram'a iletir.

Bağlı servisler:
- [aski-water-watch](https://github.com/bmuftuoglu/aski-telegram-bot) — ASKİ su kesintisi takibi

## Gereksinimler

- [Docker](https://docs.docker.com/get-docker/) ve [Docker Compose](https://docs.docker.com/compose/install/)
- Bir Telegram hesabı

## Kurulum

### 1. Paylaşılan Docker network'ü oluştur

Tüm servisler bu network üzerinden haberleşir. Bir kez oluşturulur, sunucu yeniden başlasa bile korunur.

```bash
docker network create homebot
```

### 2. Telegram botu oluştur

1. Telegram'da **@BotFather**'ı aç
2. `/newbot` komutunu gönder, bir isim ve kullanıcı adı belirle
3. Verilen token'ı kopyala

### 3. Telegram kullanıcı ID'ni öğren

Telegram'da **@userinfobot**'a mesaj at, sana user ID'ni söyler.

### 4. Güvenli bir iç token üret

Servisler arası iletişimi doğrulamak için rastgele bir token gerekir:

```bash
openssl rand -hex 32
```

### 5. Yapılandırma dosyasını oluştur

```bash
cp .env.example .env
```

`.env` dosyasını açıp şu değerleri doldur:

```env
TELEGRAM_BOT_TOKEN=BotFather_dan_aldigin_token
TELEGRAM_ALLOWED_USER_IDS=telegram_user_id
TELEGRAM_DEFAULT_CHAT_ID=telegram_user_id
INTERNAL_API_TOKEN=openssl_ile_uretilen_token
```

`TELEGRAM_ALLOWED_USER_IDS` birden fazla kullanıcı için virgülle ayrılabilir: `123,456`.

### 6. Gateway'i başlat

```bash
docker compose up --build -d
```

### 7. Servisleri bağla

Gateway çalıştıktan sonra bağımsız servisleri ayrı ayrı başlatabilirsin:

```bash
git clone https://github.com/bmuftuoglu/aski-telegram-bot
cd aski-telegram-bot
cp .env.example .env  # doldurup kaydet
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

| Değişken | Açıklama |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | BotFather'dan alınan token. |
| `TELEGRAM_ALLOWED_USER_IDS` | Botu kullanabilecek user ID'leri (virgülle). |
| `TELEGRAM_DEFAULT_CHAT_ID` | Bildirimlerin gönderileceği chat ID. |
| `INTERNAL_API_TOKEN` | Servisler arası iletişim için gizli token. |

## Yeni Servis Eklemek

Her yeni servis bağımsız bir repo ve container olarak çalışır. Dil fark etmez (Python, Node.js, Go...).

### 1. Servis bildirim gönderimini uygula

Servisin, durum değiştiğinde gateway'in `/notify` endpoint'ine POST atması gerekir:

```
POST http://telegram-bot-gateway:8080/notify
Authorization: Bearer <INTERNAL_API_TOKEN>
Content-Type: application/json

{ "text": "Bildirim metni" }
```

### 2. Servisi `homebot` network'üne bağla

Servisin `docker-compose.yml`'inde network'ü external olarak tanımla:

```yaml
networks:
  homebot:
    external: true
```

### 3. Servis URL'ini `.env`'e ekle

```env
YENI_SERVIS_URL=http://yeni-servis:8082
```

### 4. Gateway'e Telegram komutları ekle

`services/telegram-bot-gateway/src/app.py` dosyasına handler ekle:

```python
async def yeni_servis_durum(update, context):
    settings = context.application.bot_data["settings"]
    if not _is_allowed(update, settings): await _deny(update); return
    data = await _call_service(settings, f"{settings.yeni_servis_url}/status", "GET")
    await update.message.reply_text(str(data), parse_mode=None)

application.add_handler(CommandHandler("yeni_servis_durum", yeni_servis_durum))
```

### 5. Gateway'i yeniden başlat

```bash
docker compose up --build -d
```

## Güvenlik

- `.env` dosyasını asla Git'e commit etme.
- `INTERNAL_API_TOKEN` tahmin edilemez olmalı, `openssl rand -hex 32` ile üret.
- Gateway yalnızca `TELEGRAM_ALLOWED_USER_IDS` listesindeki kullanıcıların komutlarını işler.
