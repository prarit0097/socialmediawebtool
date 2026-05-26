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
- optional app-admin credentials ke saath dashboard access ko protect kar sakti hai
- cached media based publishing readiness audit aur metrics export command de sakti hai

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
- existing DB posting progress ko reset/clear kiye bina current scheduler state se hi aage continue karna hai.

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

### Publish Readiness Audit
```powershell
.\.venv\Scripts\python.exe manage.py audit_publish_readiness
```

### Export Tool Post Metrics
```powershell
.\.venv\Scripts\python.exe manage.py export_post_metrics --days 7 --output .\metrics.csv
```

### Compare Manual vs Tool Metrics
```bash
cd /opt/drive-to-meta-scheduler/app
./.venv/bin/python manage.py export_post_metrics --target-id 60 --days 30 --manual-csv /tmp/manual.csv --output /tmp/compare.csv
```

## VPS Live Deploy Commands

Ye project currently VPS par is folder se live chal raha hai:

```bash
/opt/drive-to-meta-scheduler/app
```

Live `systemd` services:

```bash
socialposter-web.service
socialposter-scheduler.service
```

### VPS Par Latest Code Pull Karna
```bash
cd /opt/drive-to-meta-scheduler/app
git pull origin main
```

### VPS Par Web + Scheduler Restart Karna
```bash
systemctl restart socialposter-web.service
systemctl restart socialposter-scheduler.service
```

### VPS Par Deploy Verify Karna
```bash
cd /opt/drive-to-meta-scheduler/app
git rev-parse --short HEAD
systemctl status socialposter-web.service --no-pager
systemctl status socialposter-scheduler.service --no-pager
```

### VPS Par Manual Telegram Report Test
```bash
cd /opt/drive-to-meta-scheduler/app
. .venv/bin/activate
python manage.py send_daily_report --date 2026-03-28 --force
```

### VPS Par Logs Check Karna
```bash
journalctl -u socialposter-web.service -n 50 --no-pager -l
journalctl -u socialposter-scheduler.service -n 50 --no-pager -l
```

### Hostinger VPS Par Django Checks Chalana
```bash
cd /opt/drive-to-meta-scheduler/app
./.venv/bin/python manage.py check
./.venv/bin/python manage.py audit_publish_readiness
```

### Hostinger VPS Par Metrics Export
```bash
cd /opt/drive-to-meta-scheduler/app
./.venv/bin/python manage.py export_post_metrics --target-id 60 --days 7 --output /tmp/target60_tool.csv
```

### Hostinger VPS Par nginx Proxy Verify Karna
```bash
nginx -t
systemctl reload nginx
journalctl -u socialposter-web.service -n 50 --no-pager -l
```

### VPS Par Full Safe Deploy Sequence
```bash
cd /opt/drive-to-meta-scheduler/app
git pull origin main
systemctl restart socialposter-web.service
systemctl restart socialposter-scheduler.service
git rev-parse --short HEAD
systemctl status socialposter-web.service --no-pager
systemctl status socialposter-scheduler.service --no-pager
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
- Hostinger VPS deployment latest repo commits tak sync ki ja chuki hai
- Hostinger nginx `X-Forwarded-Proto` forwarding fix ho chuki hai, isliye secure redirect loop issue resolve ho gaya
- Metrics export aur manual-vs-tool compare workflow live VPS par verify ho chuka hai
- Meta token sync ab non-destructive hai: naya/updated token save karne par same Facebook/Instagram page IDs ki existing Drive folder mapping, posting times, captions, AI settings, active status, cached media, aur post history preserve rehti hai. Sirf genuinely new pages/profiles new setup rows ke roop me add honge.
- Multi-token sync ab safer hai: agar new Meta token me koi page/account pehle se kisi aur saved token ke under configured hai, app us existing target ko move ya overwrite nahi karegi. Duplicate ko skip karke original target ki settings, token ownership, cached media, aur history same rakhegi.
- Code review ke baad extra hardening bhi active hai: `caption.txt` ab public Google URL se nahi balki service-account Drive download se read hota hai, `PUBLIC_APP_BASE_URL` ko real HTTPS URL hona zaroori hai, Meta sync non-JSON response ko clean error me dikhata hai, aur Facebook binary upload large video ko memory me poora load kiye bina stream karta hai.

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
- Facebook Graph response parsing ko harden kiya gaya hai. Agar Meta empty ya non-JSON response de, to app generic `Expecting value...` parser error dikhane ke bajay actual HTTP status aur response snippet log karegi.
- `Test Post Now` ko background mode me shift kiya gaya hai. Ab button click karte hi browser request latakne ke bajay page turant redirect ho jata hai, aur posting backend thread me chalti hai. Status dekhne ke liye page refresh ki ja sakti hai.
- Target/dashboard health checks ko short-lived cache diya gaya hai. Ab har page refresh par Google Drive folder ko repeatedly scan nahi kiya jayega, jis se live UI noticeably faster feel honi chahiye.
- Instagram image pipeline ko aur strict banaya gaya hai: EXIF orientation normalize hoti hai, oversized images downscale hoti hain, output baseline JPEG me convert hota hai, metadata strip hoti hai, aur file ko smaller target size ke andar compress kiya jata hai.
- Instagram image posting race condition fix ki gayi hai. Ab image containers ko bhi video ki tarah publish se pehle `ready/finished` state tak wait kiya jata hai, taaki `Media ID is not available` jaisi early-publish failures kam ho.
2026-03-25
- Telegram daily report format ko user-requested layout ke hisaab se update kiya gaya: `SUCCESSFUL ACTIVITY ---` section me ab active target blocks numbered form me aate hain aur har target ke neeche separate `fb posting` aur `insta posting` status lines `done/not done` ke saath date-time dikhati hain.
- Telegram daily report ko aur user-friendly banaya gaya: ab har active target `PAGE X: ...` heading ke saath aata hai, aur Facebook/Instagram sections me pure din ki sabhi post attempts `Post 1`, `Post 2` format me exact date-time ke saath list hoti hain.
- Telegram daily report layout ko aur professional summary style me polish kiya gaya: ab `ACTIVITY DETAILS` section me har target `TARGET X` heading ke saath aata hai, aur Facebook/Instagram ke liye post count, full-day publish timeline, aur failed attempts ka concise summary dikhata hai.
2026-03-29
- **Facebook posting ko direct binary upload (source param) par switch kiya gaya.** Pehle app URL-based upload (`url`/`file_url` param) use karti thi jisme Meta aapki URL se file fetch karta tha — is method se Meta algorithm lower distribution deta hai. Ab file directly binary multipart upload hoti hai (official FB/IG apps ki tarah), jo better algorithmic reach deti hai. URL-based upload ab sirf fallback ke roop me kaam karega.
- **Instagram image quality significantly improve ki gayi:**
  - JPEG quality 92 se 95 ki gayi (closer to original quality).
  - Chroma subsampling `4:2:0` se `4:4:4` ki gayi (full colour detail preserve hoti hai ab).
  - ICC colour profile ab preserve hota hai (pehle strip ho raha tha, jisse colours shift hote the Instagram par).
  - Max file size limit 4 MB se 8 MB ki gayi (Instagram actual limit 8 MB hai).
  - Aspect ratio enforcement add ki gayi: agar image Instagram ke 4:5 to 1.91:1 range se bahar hai to white padding se fix hogi (pehle IG reject ya poorly crop kar deta tha).
- **Caption fallback fix:** Pehle agar koi caption nahi milta (na AI, na default, na caption.txt) to filename (e.g. "IMG_20230415.jpg") caption ban jata tha — ye automated post ka signal deta tha algorithm ko aur engagement drastically kam karta tha. Ab empty caption jayega jo much better hai.
- **Telegram daily report oversized-message fix:** Agar report Telegram `sendMessage` limit ke paas pahunchti hai, to app ab report ko multiple safe chunks me split karke bhejti hai, isliye large activity days par `400 Bad Request` ki wajah se report fail nahi honi chahiye.
- All 33 existing tests pass with these changes.
2026-03-30
- `SOCIAL_POSTER.md` aur `README.md` me exact VPS deployment commands add ki gayi hain, including live app folder `/opt/drive-to-meta-scheduler/app`, `git pull`, `systemctl restart`, version verification, manual Telegram report test, aur logs check commands.
2026-04-10
- No-DB-touch hardening pass add kiya gaya: live scheduling progression aur existing posting history ko preserve rakhte hue code ko secure/configurable side par tighten kiya gaya.
- Dashboard aur target settings pages ke liye optional app-level basic-auth gate add ki gayi. Agar `APP_ADMIN_USERNAME` aur `APP_ADMIN_PASSWORD` env me set hon, to panel protected ho jayega.
- Production safety env knobs add kiye gaye: SSL redirect, secure cookies, HSTS, forwarded-proto handling, legacy public-media fallback toggle, aur AI niche mapping config.
- Publishing path me compliance preflight add hua: missing tokens, unsupported media states, aur Instagram ke liye missing public HTTPS base ko publish se pehle detect karke clear block reason diya jata hai.
- Google Drive file ko public `anyone` permission dene wali normal publish step hata di gayi. App ab cached app-hosted media path ko prefer karti hai; old proxy/Drive URL fallback sirf explicit env toggle ke saath use hoga.
- AI prompt ko hardcoded health/wellness context se hatakar configurable niche-based guidance par shift kiya gaya, taaki different targets ke liye zyada natural/original caption guidance mil sake.
- New no-DB tooling add hua:
  - `manage.py audit_publish_readiness`
  - `manage.py export_post_metrics --output ...`
- Automated coverage 33 se badhkar 40 tests tak gayi, jisme admin auth gate, compliance warnings/blocks, aur metrics export workflow cover hua.
- Facebook metrics exporter ko reel/video IDs ke liye harden kiya gaya. Ab page-post ID aur video/reel ID ko alag handle karke exporter `reactions` field error se bachti hai aur reel-style Facebook publishes ke liye metrics resolve karne ki better koshish karti hai.
- Facebook video publish payload se raw filename-derived `title` hata diya gaya. Ab tool publish ke dauran noisy file-name metadata Facebook reel object par leak nahi hota; focus sirf human caption/description par rehta hai.
- Instagram-oriented AI hygiene tighten ki gayi: noisy automation-style filenames (`viral`, `official`, serial numbers, template-like names) ko AI context se clean kiya jata hai, taaki captions aur insight generation raw spammy file names ko copy na karein.
2026-04-11
- Hostinger VPS deployment flow ko live server reality ke hisaab se verify kiya gaya: app folder `/opt/drive-to-meta-scheduler/app`, nginx site file `/etc/nginx/sites-enabled/socialposter`, aur systemd services `socialposter-web.service` / `socialposter-scheduler.service` confirm hue.
- Hostinger nginx me `proxy_set_header X-Forwarded-Proto $scheme;` forwarding ensure karke secure redirect loop issue resolve kiya gaya.
- `python` ke bajay VPS par `./.venv/bin/python` use karne wala documented command flow confirm hua.
- Real metrics export aur comparison workflow live par validate hua.
- `NirogiDhara` target ke liye:
  - Facebook manual-vs-tool gap significantly larger mila.
  - Instagram manual-vs-tool gap smaller but real mila.
  - Facebook reel metadata aur Instagram automation-style filename context reduce karne wale changes push kiye gaye.
2026-05-21
- Meta token sync ko non-destructive banaya gaya. Ab app target ko sirf `sync_key` se nahi, balki stable Facebook/Instagram external IDs se match karti hai, isliye new/updated token ke saath existing scheduling settings wipe nahi hoti.
- Facebook-only target agar later connected FB+IG pair ban jaye to wahi existing row upgrade hoti hai aur Drive folder, exact posting times, caption, AI settings, active status, cached assets, aur post logs preserve rehte hain.
- Sync response me kisi purane configured target ke temporarily missing hone par app usko deactivate/delete nahi karegi; row par clear warning save hogi aur existing scheduling safe rahegi.
- Dashboard sync message ab batata hai kitne existing targets preserve/reuse hue aur kitne new targets add hue.
- Meta sync preservation ke regression tests add hue: same pair under new token, FB-only to FB+IG upgrade, duplicate prevention, missing response preservation, aur new page creation.
- Full code review ke baad extra fixes add hue:
  - `caption.txt` reading ko service-account Drive download par shift kiya gaya, taaki private/non-public Drive folder setup me captions break na hon.
  - Public media base validation strict hui: Instagram/cached media ke liye `PUBLIC_APP_BASE_URL` ab HTTPS public URL hi maana jayega.
  - Meta account sync me empty/non-JSON Graph response ab raw parser crash ke bajay readable `MetaAPIError` ban kar dikhega.
  - Facebook direct binary upload ab local cached media file ko stream karta hai, large video ke liye unnecessary memory spike kam hota hai.
2026-05-22
- Multi Meta token safe sync implement hua. Ab multiple Meta tokens save ho sakte hain, sabke new pages/accounts same dashboard window me dikhte hain, aur cross-token duplicate page/account existing configured target ko overwrite ya move nahi karta.
- Sync result message me ab `created`, `reused`, `skipped`, aur `missing` counts clearly dikhte hain. `skipped` ka matlab page/account pehle se kisi saved token ke under configured hai.
- Dashboard target rows par ab token ownership label dikh raha hai, taaki clear ho ki kaunsa page/account kis saved Meta token se publish karega.
- Safe Meta-policy readiness warnings ko expand kiya gaya: Instagram unsupported video type aur filename-like default caption jaise issues dashboard health me advisory warning ke roop me dikhte hain, bina existing publish behavior ko unnecessarily strict banaye.
- Regression tests add/update hue for second-token new page creation, cross-token duplicate skip, FB-only duplicate skip, sync skipped count, dashboard token labels, aur readiness warnings.
2026-05-26
- VPS investigation me scheduler/web service healthy mile. Root issue scheduling service nahi tha: Riya/Riims jaise targets me Instagram media container create ho raha tha, lekin container status polling (`/{container_id}?fields=status_code,status`) par Meta `Authorization Error` de raha tha.
- Instagram publish flow me safe fallback add hua: agar container create success ke baad sirf status-poll authorization error aaye, app short wait ke baad direct `media_publish` retry karegi. Normal status polling available ho to wahi use hoti rahegi.
- Same-file queue behavior preserve hai: Facebook success ke baad Instagram pending ho to app wahi current file retry karegi; next file tabhi pick hogi jab active platforms complete ho jayen.
- Existing scheduling, Drive folders, posting times, token ownership, cached media, aur post history ko touch nahi kiya gaya.
- Tests 59 tak update hue, including Instagram status-poll auth fallback and direct-publish retry coverage.
- Meta `Application request limit reached` ke liye backoff guard add hua. Agar kisi target/platform/file par recent rate-limit failure ho, app configured backoff window tak Meta ko baar-baar hit nahi karegi, taaki scheduler repeated API calls se limit ko aur extend na kare.
- `META_RATE_LIMIT_BACKOFF_MINUTES` env knob add hua, default `90` minutes. Existing queue/progress same rahegi; backoff ke baad same pending file retry hogi.
- Tests 60 tak update hue, including rate-limit backoff coverage that verifies no extra Meta call or duplicate failure log is created during the backoff.
- Full reliability hardening pass add hua:
  - Meta Graph error messages ab sirf generic text nahi dikhate; agar Meta response me `type`, `code`, `subcode`, ya `fbtrace_id` aaye to diagnostics me preserve hote hain.
  - Backoff guard rate-limit ke saath repeated transient Meta failures par bhi kaam karta hai, jaise status-poll authorization errors, container/media-not-ready responses, timeouts, aur temporary request failures. Iska purpose Meta ko baar-baar hit karke limit badhana nahi hai.
  - Scheduler result ab `checked`, `success`, `failed`, `skipped`, `backoff`, aur `content_exhausted` counts dikhata hai. `Success=0 Failed=0` jaisi confusing output ke bajay ab skipped reason clear hoga.
  - `audit_publish_readiness` ab production audit jaisa output deta hai: token owner, active platforms, public URL readiness, Drive/media/cached counts, current queue file, pending platform, daily slots, latest success/failure, backoff state, aur content exhausted state.
  - Dashboard aur target detail page par current queue file, pending platform, backoff active, content exhausted, aur next slot hints visible hain.
  - Meta sync ab normal publish failure context ko clear nahi karta. Sirf sync-generated missing/merge warning resolve hone par clear hoti hai, taaki real posting error investigation ke dauran important `last_error` wipe na ho.
  - Same-file queue, existing Drive folders, posting times, token ownership, cached media, aur post history unchanged rakhe gaye.
  - Tests 69 tak update hue, covering detailed Meta errors, transient backoff, no duplicate logs during backoff, content exhaustion counts, audit output, dashboard hints, and sync preserving publish errors.
  - VPS production `.env` me HTTPS redirect/admin auth enabled hone par bhi test suite stable rahe, iske liye dashboard/target view tests ko explicit test settings ke saath harden kiya gaya.
  - Scheduler heartbeat output ko force-flush kiya gaya taaki systemd journal me `Scheduler started` aur periodic `Scheduler heartbeat ...` lines immediately visible hon.
