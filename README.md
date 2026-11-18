# Memora OS

Yagona foydalanuvchi (PDP University talabasi, Toshkent) uchun Telegram'dagi avtonom akademik yordamchi.
Bot barcha javoblarni **o'zbek tilida (lotin)** beradi; kirishda o'zbek (lotin/kirill), rus va ingliz tillarini tushunadi.

Arxitektura: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Tez ishga tushirish

Talab: Docker + Docker Compose, VPS yoki kompyuter (1 vCPU / 1 GB yetadi).

1. **Telegram bot** — [@BotFather](https://t.me/BotFather) → `/newbot` → tokenni oling.
2. **Gemini kaliti** — <https://aistudio.google.com/apikey>.
3. **Google (Calendar + Tasks)** — pastdagi bo'limga qarang.
4. Sozlamalar:
   ```bash
   cp .env.example .env
   # TELEGRAM_BOT_TOKEN, GEMINI_API_KEY, POSTGRES_PASSWORD, GOOGLE_* qiymatlarini to'ldiring
   ```
5. Ishga tushirish:
   ```bash
   docker compose up -d --build
   docker compose logs -f bot worker
   ```
   `migrate` xizmati sxemani avtomatik yangilaydi, so'ng `bot` va `worker` ishga tushadi.
6. Telegram'da botga `/start` yozing. Faqat `OWNER_CHAT_ID` (812304675) ga javob beriladi, qolganlar e'tiborga olinmaydi.

## Google OAuth sozlash

1. [Google Cloud Console](https://console.cloud.google.com/) → yangi loyiha.
2. **APIs & Services → Library**: *Google Calendar API* va *Google Tasks API* ni yoqing.
3. **OAuth consent screen**: User type *External*, test user sifatida `maxmanazar025@gmail.com` ni qo'shing.
   **Muhim:** ishlab chiqarishda *Publish app → In production* qiling, aks holda refresh token **7 kundan keyin** eskiradi.
4. **Credentials → Create OAuth client ID → Desktop app**. `client_id` va `client_secret` ni `.env` ga yozing.
5. Refresh token olish (o'z kompyuteringizda):
   ```bash
   pip install -r requirements.txt
   python scripts/google_auth.py
   ```
   Brauzerda `maxmanazar025@gmail.com` bilan kiring; chiqqan `GOOGLE_REFRESH_TOKEN` ni `.env` ga qo'ying.
6. `docker compose up -d` ni qayta ishga tushiring.

Google sozlanmasa ham bot ishlaydi: eslatmalar bazada saqlanadi, sinxronlash o'tkazib yuboriladi.

## Asosiy sozlamalar

| O'zgaruvchi | Ma'nosi |
|---|---|
| `TIMETABLE_SHEET_URLS` | Jadval varaqlarining CSV eksport havolalari (vergul bilan) |
| `TIMETABLE_GROUPS` | Kuzatiladigan guruhlar: `25-408,Intermediate 13` |
| `LLM_SMART_CHAIN` / `LLM_FAST_CHAIN` | `provider/model` zanjiri; xato bo'lsa keyingisiga o'tadi |
| `STT_CHAIN` | Ovozli xabarlar: `gemini,groq,openai` |
| `BRIEFING_TIME`, `CLASS_ALERT_MINUTES`, `WEEKLY_DIGEST_TIME` | Ertalabki xulosa, dars oldidan ogohlantirish, yakshanba hisoboti |
| `HOMEWORK_AUTO_CREATE` | Dars tahlilidan uy vazifasini avtomatik task qilish |

To'liq ro'yxat — `.env.example`.

## Buyruqlar

```bash
docker compose exec bot python -m memora sync          # jadvalni hozir sinxronlash
docker compose exec bot python -m memora healthcheck  # holat tekshiruvi
docker compose run --rm migrate                        # migratsiyalar
python -m memora all                                   # bot + worker bitta jarayonda (dev)
```

Telegram'da: `/start`, `/help`, `/darslar`, `/sync`, `/pauza`, `/davom`, `/status`, `/cancel`. Qolgan hammasi (bugun, ertaga, vazifalar, o'qish rejimi) menyu tugmalari yoki erkin matn/ovoz orqali.

## Testlar

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

Testlar tashqi tarmoqsiz ishlaydi (Telegram, LLM va Google soxta serverlar bilan almashtirilgan).

## Muammolarni bartaraf etish

- **Bot javob bermayapti** — `OWNER_CHAT_ID` to'g'rimi? `docker compose logs bot`. Boshqa ID'lardan kelgan xabarlar jimgina tashlanadi.
- **Jadval bo'sh** — `python -m memora sync` va loglarni ko'ring; varaq "Anyone with the link can view" bo'lishi kerak.
- **Google 401 / invalid_grant** — consent screen "Testing" holatida (7 kunlik token) yoki token bekor qilingan; `scripts/google_auth.py` ni qayta ishga tushiring.
- **LLM xatolari** — worker egasiga ogohlantirish yuboradi; zanjirdagi keyingi model avtomatik ishlatiladi.
- **Ovozli xabar tushunilmayapti** — konteynerda `ffmpeg` borligini tekshiring (Dockerfile'da o'rnatilgan).

## Ma'lum cheklovlar

- Haftalik jadval bayramlarni bilmaydi; bekor qilingan darslarni jadval o'zgarishidan aniqlaydi.
- Quiz javoblari LLM tomonidan yaratiladi va faktlar bo'yicha tekshirilmaydi.
- Yuborish "kamida bir marta emas, ko'pi bilan bir marta" printsipida: jarayon yuborishdan oldin qulasa, bitta eslatma o'tib ketishi mumkin.
