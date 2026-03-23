# Social Poster

## Ye Web App Kya Karta Hai
Ye ek Django web app hai jo Google Drive ke folder se files uthakar Facebook Page aur Instagram account par automatically post karne ke liye banaya gaya hai.

Simple words me:
- aap Meta access token dete ho
- app aapke Facebook Page aur Instagram account dikhata hai
- aap har page/profile ke saamne Google Drive folder link karte ho
- aap decide karte ho ek din me kitni posts jayengi aur kis time jayengi
- app un times par post bhejne ki koshish karta hai
- subah Telegram par report bhi bhej sakta hai

## App Abhi Kya-Kya Kar Sakti Hai
- web page se Meta token save kar sakti hai
- token se connected Facebook aur Instagram accounts sync kar sakti hai
- FB + IG connected pairs ko ek saath dikha sakti hai
- unconnected accounts ko alag dikha sakti hai
- har target ke liye Google Drive folder set kar sakti hai
- har target ke liye `posts per day` set kar sakti hai
- har post ka exact time set kar sakti hai
- `Test Post Now` button se manual test post kar sakti hai
- Facebook ke liye image aur video dono try kar sakti hai
- Instagram ke liye image aur video/reels flow try kar sakti hai
- Telegram par daily report bhej sakti hai
- recent logs aur health status dikha sakti hai

## App Ka Flow Kaise Kaam Karta Hai
1. User Meta token add karta hai.
2. App Facebook pages aur Instagram accounts ko sync karti hai.
3. User kisi target ke saath Google Drive folder map karta hai.
4. User `posts per day` aur unki timings set karta hai.
5. Scheduler due time par Drive se file uthata hai.
6. File ko app cache/public asset me convert karke public URL banaya jata hai.
7. Facebook aur Instagram ko same file bhejne ki koshish hoti hai.
8. Success ya failure ka log save hota hai.
9. Telegram report me summary bheji ja sakti hai.

## Important Rules Jo App Follow Karti Hai
- Facebook aur Instagram same current file par rehne ki koshish karte hain.
- Next file tab tak pick nahi hoti jab tak current file dono platforms par success na ho.
- `caption.txt` file ko caption source ke roop me use kiya ja sakta hai.
- Instagram image ko app optimized JPEG me convert karke bhejne ki koshish karti hai.
- Google Drive file ko pehle cached public asset me materialize karke Meta ko stable URL diya jata hai.

## Folder Me Kaunsi Files Rakhni Chahiye
Recommended:
- `caption.txt` = post caption
- `.jpeg` / `.jpg` = image post
- `.png` = image post, app IG ke liye convert kar sakti hai
- `.mp4` = video post

## Important Reality
App bahut kuch handle karti hai, lekin Instagram posting 100% guarantee nahi hoti, kyunki:
- Meta API kabhi file reject kar sakti hai
- video specs issue ho sakta hai
- token/permissions issue ho sakta hai
- public media URL Meta side se reject ho sakta hai

Isliye app ka goal hai:
- failure ko kam karna
- exact reason dikhana
- retry aur diagnostics better banana

## Aaj Tak Kya Bana Hai

### Core System
- Django project `social_poster` create hua
- `scheduler` app create hui
- models bane for credentials, targets, logs, report logs, media assets

### Meta Side
- Meta token web UI se add hota hai
- FB/IG accounts sync hote hain
- connected aur unconnected accounts alag dikhte hain

### Google Drive Side
- Drive folder ID parse hoti hai
- folder ki files read hoti hain
- `caption.txt` detect hoti hai
- media files ko cache karke public asset banaya jata hai

### Posting Side
- Facebook photo post support
- Facebook video post support
- Instagram image post flow
- Instagram video/reels flow
- same-file queue behavior
- per-platform logs
- test-post feature

### Scheduler Side
- exact per-post timing support
- day-wise slot generation
- due-post runner
- continuous scheduler command

### Report Side
- Telegram daily report
- improved readable summary format
- manual date-based report test command

### Debugging Side
- health summary UI
- recent logs
- clearer rejection diagnostics
- file metadata hints
- probable-cause messages

### Local/Public Testing Side
- ngrok helper scripts
- public URL setup
- proxy/public media serving

### Git / Process Side
- `.gitignore` add hua
- local secrets commit se bachaye gaye
- rule save hua:
  after every meaningful change, `SOCIAL_POSTER.md` update karna hai aur code GitHub par push karna hai

## Important Files
- `AGENT.md` = project ke global rules
- `SOCIAL_POSTER.md` = simple project explanation + update history
- `.env.example` = required env variables ka sample
- `requirements.txt` = Python packages

## Useful Commands

### Server Run
```powershell
.\.venv\Scripts\Activate.ps1
python manage.py runserver
```

### Scheduler Run
```powershell
.\.venv\Scripts\Activate.ps1
python manage.py run_scheduler
```

### Manual Due Posts
```powershell
.\.venv\Scripts\python.exe manage.py run_due_posts
```

### Telegram Report Test
```powershell
.\.venv\Scripts\python.exe manage.py send_daily_report --date 2026-03-21 --force
```

### Tests
```powershell
.\.venv\Scripts\python.exe manage.py test
```

## Current Status
- Web app chal rahi hai
- Meta sync chal raha hai
- Google Drive folder reading chal rahi hai
- exact posting times save ho rahe hain
- Facebook posting ka flow kaafi stable hai
- Instagram flow improved hai but still Meta-side acceptance par depend karta hai
- Telegram reporting chal rahi hai
- GitHub push workflow set hai

## Last Update
2026-03-21
- `SOCIAL_POSTER.md` ko beginner-friendly format me rewrite kiya gaya so that project ko easily samjha ja sake.
- Professional `README.md` add ki gayi for GitHub repo overview, setup, commands, and environment configuration.
- Full app scan kiya gaya, posting flow tighten kiya gaya, aur same file par already-successful platform ko dubara repost karne se roka gaya.
2026-03-22
- Scheduler ko single-instance guard diya gaya taaki duplicate/background scheduler processes ki wajah se false alerts ya double runs na aayein.
- Slot progression logic ko fix kiya gaya taaki `9 AM`, `10 AM` jaise exact times apne-apne slot ke hisaab se hi chalein aur ek slot multiple files kha kar next slot ko confuse na kare.
- Target `Riya Arora + Riya Arora` ke liye ek extra `4:00 PM IST` slot add kiya gaya. Current exact times: `09:00`, `10:00`, `16:00`, `18:00`, `19:00`.
- Unique-content rule tighten ki gayi: jo Drive media file ek baar sab active platforms par publish ho jaaye, woh auto-post queue me dubara reuse nahi hogi. Nayi posting ke liye nayi file chahiye hogi.
- Posting Times section me per-slot `Delete` button add kiya gaya. Slot delete karne par `posts per day` count bhi automatically kam ho jayega.
- Existing targets jinke exact `posting_times` pehle blank the, unke liye edit form ab default times auto-fill karta hai aur validation errors clearly UI par dikhata hai.
- Scheduler catch-up window add ki gayi. Ab bohot purane missed slots same din me achanak backfill nahi honge, isliye `Test Post Now` ke saath extra old-slot postings fire nahi honi chahiye.
- Daily Telegram report ke liye missed-start catch-up add kiya gaya. Agar scheduler 9 AM ke baad start ho, to bhi us din ka pending report send ho jayega.
- Telegram report send logic ko adjust kiya gaya taaki previous day ki manual test/send aaj ke scheduled 9 AM report ko block na kare.
- Google Drive folder listing me pagination add ki gayi. Ab 100 se zyada files wale folders ka real full count app health aur posting logic me dikhna chahiye.
- Dashboard top par `Active Scheduling Profiles` section add kiya gaya taaki jo profiles currently scheduling me use ho rahi hain woh sabse upar hi visible rahein.
- Video posts ke liye caption handling normalize ki gayi aur Facebook video publish payload me `title` bhi add kiya gaya, taaki text/caption visibility zyada consistent rahe.
- Dashboard data loading optimize ki gayi: ab same target ka health data ek hi baar build hota hai aur multiple sections me reuse hota hai, jis se unnecessary Drive/API work kam hota hai.
- AI foundation add ki gayi: OpenAI-compatible API key ke saath caption generation, hashtags, rewrites, translations, duplicate/quality warnings, content classification, best-time suggestions, AI media insight cache, aur AI-enhanced Telegram summary ka base ready hai.
- `.env` me AI config placeholders add kiye gaye (`AI_API_KEY`, `AI_API_BASE_URL`, `AI_MODEL`, `AI_TIMEOUT_SECONDS`, `SCHEDULER_CATCHUP_MINUTES`) taaki OpenAI key paste karke AI features turant use kiye ja sakein.
2026-03-23
- `.env` template aur local `.env` ko readable sections me organize kiya gaya: Django, Meta, Google Drive, Public App, Telegram, Scheduler, aur AI blocks alag-alag rakh diye gaye taaki config samajhna easy ho.
- AI model preference order set ki gayi: pehle `openai/gpt-4.1-nano`, aur agar wo fail ho to automatically `openai/gpt-4.1-mini` try kiya jayega.
- AI service me fallback logic aur automated test add kiya gaya taaki primary model failure ke baad second model reliable tarike se try ho.
- AI feature audit ke baad AI request handling aur strong ki gayi: network/request failure aur invalid JSON cases ko per-model capture karke fallback model par move kiya jayega.
- Target detail page ke `AI Workspace` me ab `short/long rewrites`, `Hindi/English/Hinglish language rewrites`, aur `translation variants` alag-alag clearly dikhte hain.
- AI end-to-end flow ke liye extra tests add kiye gaye: AI insight generation, AI caption apply button, AI auto-caption publish path, aur AI report summary inclusion verify kiya gaya.
- Real OpenAI key testing ke dauran `.env` me `AI_API_KEY` aur `AI_API_BASE_URL` lines ulta paste hui mili; local config ko sahi kiya gaya.
- OpenAI-compatible base URL ke liye model-name normalization add ki gayi: agar config me `openai/gpt-4.1-nano` ya `openai/gpt-4.1-mini` likha ho to OpenAI API call me automatically `gpt-4.1-nano` aur `gpt-4.1-mini` send kiya jayega.
- Live provider verification me minimal JSON response aur AI-enhanced daily report summary dono successful chale.
- Google `api_core` ka Python 3.10 `FutureWarning` targeted tarike se suppress kiya gaya taaki runtime output clean rahe, bina project behavior change kiye.
- AI insight raw payload me ab provider/model metadata bhi save hoti hai, taaki target page par dikh sake kaunsa AI model actually use hua tha.
- Target detail page par AI model metadata dikhate waqt Django template underscore-key access error aa rahi thi; metadata ko view context me flatten karke issue fix kiya gaya.
- Ollama support remove kar di gayi aur AI layer ko phir se pure OpenAI mode me simplify kiya gaya. Ab app `openai/gpt-4.1-nano` ko primary aur `openai/gpt-4.1-mini` ko fallback model ke roop me use karti hai.
- Weak ya malformed AI output ke liye normalization aur quality gate active rakhi gayi hai, taaki poor structured responses ke baad same provider par better fallback model try kiya ja sake.
- Live domain par form submit karte waqt `403 CSRF verification failed` issue fix kiya gaya. Ab `PUBLIC_APP_BASE_URL` se domain automatically trusted origin ban jata hai, aur optional extra origins `DJANGO_CSRF_TRUSTED_ORIGINS` se add kiye ja sakte hain.
- Meta Graph API ke liye timeout aur retry settings add ki gayi hain. Ab Facebook/Instagram call me short network/read timeout aane par request turant fail hone ke bajay configured retry ke saath dubara try kar sakti hai.
