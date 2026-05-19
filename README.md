# Telegram Home Server

Kişisel ev otomasyonu için merkezi bir Telegram botu. Harici servislerden gelen bildirimleri Telegram'a iletir ve Telegram komutlarını iç servislere yönlendirir.

```
aski-water-watch  ──POST /notify──▶  telegram-bot-gateway  ──▶  Telegram
                                              ▲
                               kullanıcı /aski_durum komutu
```

Bu proje yalnızca gateway'i yönetir. Servisler bağımsız birer proje olarak çalışır ve paylaşılan `homebot` Docker network'ü üzerinden iletişim kurar.

## Servisler

- **telegram-bot-gateway** — Telegram botunu yönetir, komutları karşılar, kullanıcı doğrulaması yapar, iç servislerden gelen bildirimleri Telegram'a iletir.

Bağlanabilecek servisler:
- [aski-water-watch](https://github.com/bmuftuoglu/aski-telegram-bot) — ASKİ su kesintisi takibi

## Gereksinimler

- [Docker](https://docs.docker.com/get-docker/) ve [Docker Compose](https://docs.docker.com/compose/install/)
- Bir Telegram hesabı

## Kurulum

### 1. Paylaşılan Docker network'ü oluştur

Tüm servisler bu network üzerinden haberleşir. Bir kez oluşturulur.

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

Servisler arası iletişimi şifrelemek için rastgele bir token gerekir:

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

### 6. Başlat

```bash
docker compose up --build -d
```

### 7. Servisleri bağla

Gateway çalıştıktan sonra bağımsız servisleri ayrı ayrı başlatabilirsin. Örneğin ASKİ su kesintisi takibi için:

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

Her yeni servis ayrı bir repo/container olarak çalışır. Dil fark etmez (Python, Node.js, Go...).

1. Servis, bildirimleri gateway'in `/notify` endpoint'ine POST ile gönderir:
   ```json
   { "text": "Bildirim metni" }
   ```
   Header: `Authorization: Bearer $INTERNAL_API_TOKEN`
2. Servisin `docker-compose.yml`'inde `homebot` network'ünü `external: true` olarak tanımla
3. `telegram-bot-gateway/src/app.py`'ye komut handler'ı ekle

## Güvenlik

- `.env` dosyasını asla Git'e commit etme.
- `INTERNAL_API_TOKEN` tahmin edilemez olmalı, `openssl rand -hex 32` ile üret.
- Gateway yalnızca `TELEGRAM_ALLOWED_USER_IDS` listesindeki kullanıcıların komutlarını işler.
