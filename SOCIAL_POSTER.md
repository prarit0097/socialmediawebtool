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
