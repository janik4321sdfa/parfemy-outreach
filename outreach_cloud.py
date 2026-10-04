# -*- coding: utf-8 -*-
"""Parfemy outreach - cloudova verze (GitHub Actions).

Stav (kontakty, odeslane maily, odpovedi, sablony) je v state.sqlite, ktery se
v repu uklada jen ZASIFROVANY (state.enc na vetvi `state`, viz statebox.py).

  python outreach_cloud.py loop --minutes 330   hlavni smycka (posila + kontroluje postu)
  python outreach_cloud.py send --max 30         jedna davka
  python outreach_cloud.py check                 precte postu, ANO -> Discord
  python outreach_cloud.py stats                 souhrn

Promenne prostredi: GMAIL_APP_PASSWORD, DISCORD_WEBHOOK, STATE_DB (default state.sqlite),
SAVE_CMD (prikaz pro prubezne ulozeni stavu do repa).
"""
import sys, os, re, json, time, random, sqlite3, smtplib, imaplib, email, datetime, urllib.request, urllib.error
from email.message import EmailMessage
from email.utils import make_msgid, formatdate, parseaddr
from email.header import decode_header, make_header

DB = os.environ.get("STATE_DB", "state.sqlite")
QUOTA_24H = 400          # Gmail osobni ucet = tvrdy limit 500 / klouzavych 24 h
PAUSE = (20, 50)         # pauza mezi maily [s]
BATCH = 30               # po kazde davce se stav ulozi do repa


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S ") + " ".join(str(x) for x in a), flush=True)


def db():
    c = sqlite3.connect(DB, timeout=60)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS targets(slug TEXT PRIMARY KEY, name TEXT, country TEXT, email TEXT, nperf INT);
    CREATE TABLE IF NOT EXISTS outbox(slug TEXT PRIMARY KEY, email TEXT, lang TEXT, subject TEXT,
        message_id TEXT, sent_at TEXT, status TEXT);
    CREATE TABLE IF NOT EXISTS replies(uid TEXT PRIMARY KEY, slug TEXT, from_addr TEXT, subject TEXT,
        received_at TEXT, category TEXT, snippet TEXT, notified INT DEFAULT 0);
    CREATE TABLE IF NOT EXISTS seen(uid TEXT PRIMARY KEY);
    """)
    return c


def cfg(c):
    return json.loads(c.execute("select value from meta where key='config'").fetchone()[0])


def password():
    p = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "")
    if not p:
        sys.exit("Chybi GMAIL_APP_PASSWORD")
    return p


# ------------------------------------------------------------------ sablony
def lang_for(country):
    return "cs" if country in ("Czech Republic", "\u010cesk\u00e1 republika", "Slovakia", "Slovensko") else "en"


def clean_brand(b):
    try:
        b = b.encode("latin-1").decode("utf-8")
    except Exception:
        pass
    return re.sub(r"[\u00ae\u2122\u00a9]", "", b).strip()


def render(brand, lang, conf):
    brand = clean_brand(brand)
    name = conf["sender_name"]
    if lang == "cs":
        return f"Dotaz na vzorek parf\u00e9mu {brand}", conf["template_cs"].format(brand=brand, name=name)
    return f"Sample request - {brand}", conf["template_en"].format(brand=brand, name=name)


def targets(c, limit=None):
    rows = c.execute("""select t.slug, t.name, t.country, t.email from targets t
        left join outbox o on o.slug=t.slug where o.slug is null
        order by (t.country in ('Czech Republic','Slovakia')) desc, t.nperf desc""").fetchall()
    sent = {r[0].lower() for r in c.execute("select email from outbox")}
    seen, out = set(), []
    for slug, name, country, em in rows:
        k = em.lower()
        if k in seen or k in sent:
            continue
        seen.add(k)
        out.append((slug, name, country, em))
        if limit and len(out) >= limit:
            break
    return out


def quota_left(c):
    since = (datetime.datetime.now() - datetime.timedelta(hours=24)).isoformat(timespec="seconds")
    n = c.execute("select count(*) from outbox where sent_at >= ?", (since,)).fetchone()[0]
    return max(0, QUOTA_24H - n), n


def next_slot(c):
    """Kdy se uvolni dalsi misto v klouzavem 24h limitu."""
    since = (datetime.datetime.now() - datetime.timedelta(hours=24)).isoformat(timespec="seconds")
    r = c.execute("select sent_at from outbox where sent_at >= ? order by sent_at limit 1", (since,)).fetchone()
    if not r:
        return datetime.datetime.now()
    return datetime.datetime.fromisoformat(r[0]) + datetime.timedelta(hours=24, seconds=5)


# ------------------------------------------------------------------ odesilani
def set_pause(c, hours):
    c.execute("INSERT OR REPLACE INTO meta VALUES('pause_until', ?)",
              ((datetime.datetime.now() + datetime.timedelta(hours=hours)).isoformat(timespec="seconds"),))
    c.commit()


def cmd_send(max_n=BATCH, deadline=None):
    c = db()
    conf = cfg(c)
    left, done = quota_left(c)
    n = min(left, max_n)
    if n <= 0:
        log(f"send: za 24 h odeslano {done}/{QUOTA_24H} - limit vycerpan, cekam")
        return 0
    todo = targets(c, n)[:n]
    log(f"send: za 24 h odeslano {done}/{QUOTA_24H}, v teto davce {len(todo)}, zbyva v seznamu {len(targets(c))}")
    if not todo:
        return 0
    g, p = conf["gmail"], password()
    s = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60)
    s.login(g, p)
    sent = refused_streak = 0
    for i, (slug, name, country, em) in enumerate(todo, 1):
        if deadline and time.time() > deadline:
            log("send: dosel cas behu, zbytek v pristim behu")
            break
        lang = lang_for(country)
        subj, body = render(name, lang, conf)
        msg = EmailMessage()
        msg["From"] = f'{conf["sender_name"]} <{g}>'
        msg["To"] = em
        msg["Subject"] = subj
        msg["Date"] = formatdate(localtime=True)
        mid = make_msgid(domain="gmail.com")
        msg["Message-ID"] = mid
        msg.set_content(body)
        try:
            s.send_message(msg)
            st, refused_streak = "sent", 0
        except smtplib.SMTPRecipientsRefused:
            st = "refused"
            refused_streak += 1
        except smtplib.SMTPServerDisconnected:
            s = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60)
            s.login(g, p)
            s.send_message(msg)
            st = "sent"
        except smtplib.SMTPDataError as e:
            log("SMTP data error - stop + pauza 6 h (mozny limit Gmailu):", e)
            set_pause(c, 6)
            break
        c.execute("INSERT OR REPLACE INTO outbox VALUES(?,?,?,?,?,?,?)",
                  (slug, em, lang, subj, mid, datetime.datetime.now().isoformat(timespec="seconds"), st))
        c.commit()
        sent += 1
        log(f"[{i}/{len(todo)}] {st} ({clean_brand(name)})")
        if refused_streak >= 5:
            log("5 odmitnuti po sobe - pauza 6 h")
            set_pause(c, 6)
            break
        if i < len(todo):
            time.sleep(random.uniform(*PAUSE))
    try:
        s.quit()
    except Exception:
        pass
    return sent


# ------------------------------------------------------------------ trideni odpovedi
AUTO_PAT = re.compile(
    r"out of office|automatic reply|auto.?reply|autoreply|automatick|abwesen|absence|vacation|"
    r"thank you for (contacting|reaching out|your (message|email|enquiry|inquiry))|"
    r"we.?ve received your|we have received your|received your (message|inquiry|enquiry|request)|"
    r"will (respond|reply|get back to you) (soon|shortly|within)|member of our team will|"
    r"ticket|request #|case #|casenummer|satisfaction.survey|how would you rate|"
    r"verify that you are|real live human|spam source|requires verification|"
    r"d\u011bkujeme za va\u0161i zpr\u00e1vu|dekujeme za vasi zpravu|nedoru\u010d", re.I)
YES_PAT = re.compile(
    r"\byes\b[^.?!]{0,40}\bsend|\b(sure|of course|absolutely|definitely|gladly)\b[!,. ]|"
    r"happy to send|glad to send|(would|we.?d|i.?d) love to send|"
    r"\b(we|i) (can|could|will|shall) (send|ship|mail|post)|\b(we|i).?ll (send|ship|be sending)|"
    r"sending you|send you (a |some |one |our |my )?(sample|vial|decant|something)|"
    r"(your|the) (full |shipping |postal |mailing |delivery |home )address|"
    r"(send|give|provide|share|need|want|forward|tell|write)[^.?!]{0,25}\b(your|the) address|"
    r"(full )?(data|details), address|address,? (name|phone)|"
    r"send (us|me) your (full |shipping |postal )?(address|details)|"
    r"r\u00e1d(i|a)? (v\u00e1m|ti) (po\u0161l|za\u0161l)|po\u0161lu v\u00e1m|po\u0161leme v\u00e1m|za\u0161leme v\u00e1m|"
    r"po\u0161lem v\u00e1m|za\u0161lem v\u00e1m|"
    r"(va\u0161i|va\u0161u|svoji|svoju|doru\u010dovac\u00ed|dod\u00e1vac\u00ed|po\u0161tovn\u00ed) adres|"
    r"adresu (pro|na) (zasl\u00e1n|doru\u010den)|\bano\b[^.?!]{0,30}(po\u0161l|za\u0161l)", re.I)
NO_PAT = re.compile(
    r"unfortunately|regret|not able to|unable to|we do not (offer|provide|send|ship)|"
    r"(don.?t|do not) (offer|provide|send|ship|handle)|cannot (offer|provide|send|ship)|"
    r"can.?t (offer|provide|send|ship)|no samples|not (currently|at this time)|outside (of )?the|"
    r"prefer not|aren.?t able|isn.?t possible|not possible|bohu\u017eel|nem\u016f\u017eeme|neposkytujeme|"
    r"nezas\u00edl\u00e1me|nenab\u00edz\u00edme|nepos\u00edl\u00e1me", re.I)


def is_reply(msg):
    subj = str(make_header(decode_header(msg.get("Subject", ""))))
    return bool(msg.get("In-Reply-To") or re.match(r"\s*(re|odp|aw|sv|vs|r|res)\s*:", subj, re.I))


def classify(msg, text):
    frm = parseaddr(msg.get("From", ""))[1].lower()
    if "mailer-daemon" in frm or "postmaster" in frm or msg.get("Content-Type", "").startswith("multipart/report"):
        return "bounce"
    if msg.get("Auto-Submitted", "no").lower() != "no" or msg.get("X-Autoreply") or msg.get("X-Autorespond"):
        return "auto"
    t = text[:3000]
    yes, no, auto = bool(YES_PAT.search(t)), bool(NO_PAT.search(t)), bool(AUTO_PAT.search(t))
    if yes and not no and (is_reply(msg) or not auto):
        return "yes"
    if no and not yes:
        return "no"
    if auto and not no:
        return "auto"
    return "unclear"


def body_text(msg):
    parts = []
    for part in (msg.walk() if msg.is_multipart() else [msg]):
        if part.get_content_type() in ("text/plain", "text/html") and not part.get_filename():
            try:
                t = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
            except Exception:
                continue
            if part.get_content_type() == "text/html":
                t = re.sub(r"(?is)<(style|script)[^>]*>.*?</\1>", " ", t)
                t = re.sub(r"<[^>]+>", " ", t)
            parts.append(t)
            if part.get_content_type() == "text/plain":
                break
    t = "\n".join(parts)
    t = re.split(r"\n(On .{5,80}wrote:|Dne .{5,80}napsal|Em .{5,80}escreveu|Am .{5,80}schrieb|"
                 r"Le .{5,80}a \u00e9crit|-----Original Message|From: |Od: )", t)[0]
    return re.sub(r"\s+", " ", t).strip()


# ------------------------------------------------------------------ Discord
def discord(text, embed=None):
    hook = os.environ.get("DISCORD_WEBHOOK", "")
    if not hook:
        log("DISCORD_WEBHOOK chybi - notifikace se neposle")
        return False
    body = {"username": "Parfemy", "content": text}
    if embed:
        body["embeds"] = [embed]
    req = urllib.request.Request(hook, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "parfemy-outreach"})
    for _ in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status in (200, 204)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(5)
                continue
            log("discord HTTP", e.code, e.read()[:200])
            return False
        except Exception as e:
            log("discord chyba", e)
            time.sleep(3)
    return False


def notify_yes(c):
    rows = c.execute("""select r.uid, r.slug, r.from_addr, r.subject, r.received_at, r.snippet, t.name, t.country
        from replies r left join targets t on t.slug=r.slug
        where r.category='yes' and coalesce(r.notified,0)=0""").fetchall()
    for uid, slug, frm, subj, rec, snip, name, country in rows:
        brand = clean_brand(name or slug)
        embed = {
            "title": f"\U0001F381 {brand} odpov\u011bd\u011bl(a) ANO",
            "color": 0x41A161,
            "description": (snip or "")[:1500],
            "fields": [
                {"name": "Od", "value": frm or "?", "inline": True},
                {"name": "Zem\u011b", "value": country or "?", "inline": True},
                {"name": "P\u0159edm\u011bt", "value": (subj or "?")[:250], "inline": False},
                {"name": "Otev\u0159\u00edt v Gmailu",
                 "value": f"https://mail.google.com/mail/u/0/#search/from%3A{frm}", "inline": False},
            ],
            "footer": {"text": f"p\u0159i\u0161lo {rec}"},
        }
        if discord(f"**Parf\u00e9m: ANO od {brand}!** Odepi jim ze sv\u00e9ho Gmailu.", embed):
            c.execute("update replies set notified=1 where uid=?", (uid,))
            c.commit()
            log("discord: ANO oznameno", brand)


# ------------------------------------------------------------------ cteni posty
def cmd_check():
    c = db()
    conf = cfg(c)
    out = list(c.execute("select slug, email, message_id from outbox where status='sent'"))
    by_mid = {m: s for s, e, m in out}
    by_email = {e.lower(): s for s, e, m in out}
    by_dom = {}
    for s, e, m in out:
        by_dom.setdefault(e.split("@")[1].lower(), s)
    m = imaplib.IMAP4_SSL("imap.gmail.com", 993)
    m.login(conf["gmail"], password())
    since = (datetime.date.today() - datetime.timedelta(days=60)).strftime("%d-%b-%Y")
    new = 0
    for box in ("INBOX", "[Gmail]/Spam"):
        if m.select(f'"{box}"', readonly=True)[0] != "OK":
            continue
        typ, data = m.uid("search", None, f"(SINCE {since})")
        for uid in data[0].split():
            key = f"{box}:{uid.decode()}"
            if c.execute("select 1 from seen where uid=?", (key,)).fetchone() or \
               c.execute("select 1 from replies where uid=?", (key,)).fetchone():
                continue
            typ, d = m.uid("fetch", uid, "(BODY.PEEK[])")
            if not d or not d[0] or not isinstance(d[0], tuple):
                continue
            msg = email.message_from_bytes(d[0][1])
            frm = parseaddr(msg.get("From", ""))[1].lower()
            refs = msg.get("In-Reply-To", "") + " " + msg.get("References", "")
            slug = next((by_mid[x] for x in re.findall(r"<[^>]+>", refs) if x in by_mid), None)
            text = body_text(msg)
            if not slug:
                slug = by_email.get(frm) or by_dom.get(frm.split("@")[-1])
            if not slug and ("mailer-daemon" in frm or "postmaster" in frm):
                hit = [s for e, s in by_email.items() if e in text.lower()]
                slug = hit[0] if hit else None
            if not slug or frm == conf["gmail"].lower():
                c.execute("INSERT OR IGNORE INTO seen VALUES(?)", (key,))
                continue
            cat = classify(msg, text)
            subj = str(make_header(decode_header(msg.get("Subject", ""))))
            c.execute("INSERT OR REPLACE INTO replies VALUES(?,?,?,?,?,?,?,0)",
                      (key, slug, frm, subj, msg.get("Date", ""), cat, text[:1500]))
            new += 1
            log(f"check: nova odpoved [{cat}] {slug}")
    c.commit()
    m.logout()
    log("check: novych odpovedi", new)
    notify_yes(c)


def cmd_stats():
    c = db()
    sent = c.execute("select count(*) from outbox where status='sent'").fetchone()[0]
    left, n24 = quota_left(c)
    cats = dict(c.execute("select category, count(distinct slug) from replies group by category"))
    log(f"stav: odeslano {sent}, za 24 h {n24}/{QUOTA_24H}, ve fronte {len(targets(c))}, odpovedi {cats}")


def save_state():
    cmd = os.environ.get("SAVE_CMD")
    if cmd:
        rc = os.system(cmd)
        if rc:
            log("ulozeni stavu selhalo, rc =", rc)


def cmd_loop(minutes):
    end = time.time() + minutes * 60
    last_check = 0
    while time.time() < end - 120:
        c = db()
        pu = c.execute("select value from meta where key='pause_until'").fetchone()
        c.close()
        paused = pu and datetime.datetime.fromisoformat(pu[0]) > datetime.datetime.now()
        sent = 0
        if paused:
            log("odesilani pozastaveno do", pu[0])
        else:
            try:
                sent = cmd_send(BATCH, deadline=end - 300)
            except Exception as e:
                log("send chyba:", repr(e))
        if sent or time.time() - last_check > 15 * 60:
            try:
                cmd_check()
            except Exception as e:
                log("check chyba:", repr(e))
            last_check = time.time()
            save_state()
        if not sent:
            c = db()
            wake = min(datetime.datetime.now() + datetime.timedelta(minutes=15), next_slot(c))
            c.close()
            nap = max(60, (wake - datetime.datetime.now()).total_seconds())
            nap = min(nap, end - 120 - time.time())
            if nap <= 0:
                break
            log(f"cekam {int(nap)} s")
            time.sleep(nap)
    try:
        cmd_check()
    except Exception as e:
        log("check chyba:", repr(e))
    cmd_stats()


if __name__ == "__main__":
    a = sys.argv[1] if len(sys.argv) > 1 else ""
    if a == "loop":
        cmd_loop(int(sys.argv[sys.argv.index("--minutes") + 1]) if "--minutes" in sys.argv else 330)
    elif a == "send":
        cmd_send(int(sys.argv[sys.argv.index("--max") + 1]) if "--max" in sys.argv else BATCH)
    elif a == "check":
        cmd_check()
    elif a == "stats":
        cmd_stats()
    else:
        print(__doc__)
