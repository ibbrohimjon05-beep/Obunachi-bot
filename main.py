import os,io,re,json,time,hmac,hashlib,sqlite3,asyncio,base64,threading,urllib.parse,urllib.request
from contextlib import asynccontextmanager
from datetime import datetime,timedelta,timezone
from fastapi import FastAPI,Header,HTTPException,UploadFile,File,Form,Depends
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from aiogram import Bot,Dispatcher,F
from aiogram.filters import CommandStart,CommandObject
from aiogram.types import Message,CallbackQuery,InlineKeyboardMarkup,InlineKeyboardButton,WebAppInfo,MenuButtonWebApp,BufferedInputFile
from PIL import Image
from anthropic import AsyncAnthropic

TOKEN=os.environ["BOT_TOKEN"]
ADMINS={int(x) for x in os.environ.get("ADMIN_IDS","").split(",") if x.strip()}
WEBAPP=os.environ.get("WEBAPP_URL","")
DB_PATH=os.environ.get("DB_PATH","bot.db")
TZ=int(os.environ.get("TZ_OFFSET","5"))
MODEL=os.environ.get("AI_MODEL","claude-haiku-5-5")
HERE=os.path.dirname(os.path.abspath(__file__))
bot=Bot(TOKEN);dp=Dispatcher()
ai=AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY","missing"))
BOT_ID=0;BOTNAME=""
now=lambda:int(time.time())

db=sqlite3.connect(DB_PATH,check_same_thread=False);db.row_factory=sqlite3.Row;lock=threading.Lock()
def ex(sql,a=()):
    with lock:
        c=db.execute(sql,a);db.commit();return c
def q1(sql,a=()):
    with lock:return db.execute(sql,a).fetchone()
def qa(sql,a=()):
    with lock:return db.execute(sql,a).fetchall()
with lock:
    db.executescript("""
    create table if not exists users(id integer primary key,name text,bal real default 0,joined int,ref int,terms int default 0,banned int default 0,dep real default 0,daily int default 0);
    create table if not exists orders(id integer primary key autoincrement,uid int,type text,link text,chat text,title text,qty int,rem int,reward real,unit real,status text,created int);
    create table if not exists dones(id integer primary key autoincrement,uid int,oid int,ts int,reward real,fined int default 0,unique(uid,oid));
    create table if not exists shots(hash text primary key,uid int,ts int);
    create table if not exists settings(k text primary key,v text);
    create table if not exists deps(id integer primary key autoincrement,uid int,som real,rub real,status text,ts int);
    """);db.commit()

DEF={"start_bonus":2,"daily_bonus":0.2,"ref_bonus":0.5,"rate":600,"min_dep":10000,
"card":"Karta raqami kiritilmagan","share":0.6,
"price":{"tg_sub":1.0,"tg_react":0.5,"tg_star":1.5,"yt_sub":1.5,"yt_like":0.7,"yt_comment":1.0,"ig_sub":1.2,"ig_like":0.6,"ig_comment":1.0},
"channels":[],
"banned":["porno","porn","xxx","sex","seks","18+","erotik","erotic","nude","onlyfans","hentai","xentai","yalang'och","yalangoch","escort","eskort","prostitut","секс","порно","эротик","интим","голые","проститут"],
"contest":"Konkurs: eng ko'p odam chaqirgan g'olib bo'ladi!","contest_start":0}
def S(k):
    r=q1("select v from settings where k=?",(k,));return json.loads(r["v"]) if r else DEF[k]
def banned_text(t):
    t=(t or "").lower();return any(w in t for w in S("banned"))

def check_init(init):
    try:
        d=dict(urllib.parse.parse_qsl(init,keep_blank_values=True));h=d.pop("hash")
        s="\n".join(f"{k}={v}" for k,v in sorted(d.items()))
        key=hmac.new(b"WebAppData",TOKEN.encode(),hashlib.sha256).digest()
        if not hmac.compare_digest(hmac.new(key,s.encode(),hashlib.sha256).hexdigest(),h):return None
        return json.loads(d["user"])
    except Exception:return None

def newuser(uid,name,ref=None):
    if q1("select 1 from users where id=?",(uid,)):return
    if ref==uid or not q1("select 1 from users where id=?",(ref,)):ref=None
    try:ex("insert into users(id,name,bal,joined,ref) values(?,?,?,?,?)",(uid,(name or "")[:40],S("start_bonus"),now(),ref))
    except sqlite3.IntegrityError:return
    if ref:ex("update users set bal=bal+? where id=?",(S("ref_bonus"),ref))

async def need_sub(uid):
    r=[]
    for ch in S("channels"):
        try:
            m=await bot.get_chat_member(ch,uid)
            if m.status in("left","kicked"):r.append(ch)
        except Exception:pass
    return r

async def au(x_init:str=Header("")):
    u=check_init(x_init)
    if not u:raise HTTPException(401,"Auth xato. Botni Telegram ichidan oching")
    newuser(u["id"],(u.get("first_name","")+" "+u.get("last_name","")).strip())
    r=q1("select banned from users where id=?",(u["id"],))
    if r["banned"]:raise HTTPException(403,"Siz bloklangansiz")
    return u["id"]
async def ok(uid=Depends(au)):
    if not q1("select terms from users where id=?",(uid,))["terms"]:raise HTTPException(403,"terms")
    if await need_sub(uid):raise HTTPException(403,"sub")
    return uid
async def adm(uid=Depends(au)):
    if uid not in ADMINS:raise HTTPException(403,"Ruxsat yo'q")
    return uid

def pay(uid,o):
    with lock:
        try:db.execute("insert into dones(uid,oid,ts,reward) values(?,?,?,?)",(uid,o["id"],now(),o["reward"]))
        except sqlite3.IntegrityError:
            db.rollback();raise HTTPException(400,"Allaqachon bajarilgan")
        c=db.execute("update orders set rem=rem-1 where id=? and rem>0 and status='on'",(o["id"],))
        if c.rowcount==0:
            db.rollback();raise HTTPException(400,"Buyurtma tugagan")
        db.execute("update orders set status='done' where id=? and rem=0",(o["id"],))
        db.execute("update users set bal=bal+? where id=?",(o["reward"],uid));db.commit()

def get_open(oid,uid):
    o=q1("select * from orders where id=? and status='on' and rem>0 and uid!=?",(oid,uid))
    if not o:raise HTTPException(400,"Vazifa topilmadi")
    if q1("select 1 from dones where uid=? and oid=?",(uid,oid)):raise HTTPException(400,"Allaqachon bajargansiz")
    return o

async def fine_loop():
    while True:
        await asyncio.sleep(3600)
        try:
            rows=qa("select d.id did,d.uid,d.reward,o.chat from dones d join orders o on o.id=d.oid where o.type='tg_sub' and d.fined=0 and d.ts>?",(now()-15*86400,))
            for r in rows:
                await asyncio.sleep(0.05)
                try:m=await bot.get_chat_member(r["chat"],r["uid"])
                except Exception:continue
                if m.status in("left","kicked"):
                    ex("update dones set fined=1 where id=?",(r["did"],))
                    ex("update users set bal=bal-? where id=?",(r["reward"]*2,r["uid"]))
                    try:await bot.send_message(r["uid"],f"⚠️ {r['chat']} kanalidan 15 kun ichida chiqdingiz. Jarima: {r['reward']*2} ₽")
                    except Exception:pass
        except Exception:pass

@asynccontextmanager
async def life(app):
    global BOT_ID,BOTNAME
    info=await bot.get_me();BOT_ID=info.id;BOTNAME=info.username
    await bot.delete_webhook(drop_pending_updates=True)
    if WEBAPP:
        try:await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="🔥 Ochish",web_app=WebAppInfo(url=WEBAPP)))
        except Exception:pass
    tasks_=[asyncio.create_task(dp.start_polling(bot,handle_signals=False)),asyncio.create_task(fine_loop())]
    yield
    for x in tasks_:x.cancel()

app=FastAPI(lifespan=life)

@app.get("/",response_class=HTMLResponse)
async def index():
    return open(os.path.join(HERE,"index.html"),encoding="utf8").read()

@dp.message(CommandStart())
async def start(m:Message,command:CommandObject):
    a=command.args or "";ref=int(a[4:]) if a.startswith("ref_") and a[4:].isdigit() else None
    newuser(m.from_user.id,m.from_user.full_name,ref)
    kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔥 Ochish",web_app=WebAppInfo(url=WEBAPP))]])
    await m.answer("🔥 Obunach1: obuna bo'l, pul top, o'z kanalingni o'stir!",reply_markup=kb)

@dp.callback_query(F.data.startswith("dp:"))
async def dcb(c:CallbackQuery):
    if c.from_user.id not in ADMINS:return await c.answer("Ruxsat yo'q")
    _,i,a=c.data.split(":");d=q1("select * from deps where id=?",(i,))
    if not d or d["status"]!="wait":return await c.answer("Allaqachon ko'rilgan")
    if a=="1":
        ex("update deps set status='ok' where id=?",(i,))
        ex("update users set bal=bal+?,dep=dep+? where id=?",(d["rub"],d["som"],d["uid"]))
        msg=f"✅ Depozit tasdiqlandi: +{d['rub']} ₽"
    else:
        ex("update deps set status='no' where id=?",(i,));msg="❌ Depozit rad etildi"
    try:await bot.send_message(d["uid"],msg)
    except Exception:pass
    try:await c.message.edit_caption(caption=(c.message.caption or "")+"\n"+msg)
    except Exception:pass
    await c.answer("OK")

@app.get("/api/me")
async def me(uid=Depends(au)):
    u=q1("select * from users where id=?",(uid,))
    refs=[dict(r) for r in qa("select id,name from users where ref=?",(uid,))]
    top=[dict(r) for r in qa("select u.name name,count(*) c from users x join users u on u.id=x.ref where x.joined>=? group by x.ref order by c desc limit 10",(S("contest_start"),))]
    return {"id":uid,"bal":round(u["bal"],2),"terms":bool(u["terms"]),"need_sub":await need_sub(uid),"joined":u["joined"],
    "dep":u["dep"],"done":q1("select count(*) c from dones where uid=?",(uid,))["c"],"refs":refs,"admin":uid in ADMINS,"bot":BOTNAME,
    "prices":S("price"),"card":S("card"),"rate":S("rate"),"min_dep":S("min_dep"),"ref_bonus":S("ref_bonus"),"contest":S("contest"),"top":top}

@app.post("/api/accept_terms")
async def terms(uid=Depends(au)):
    ex("update users set terms=1 where id=?",(uid,));return {"ok":True}

@app.post("/api/daily")
async def daily(uid=Depends(ok)):
    u=q1("select daily from users where id=?",(uid,))
    if now()-u["daily"]<86400:raise HTTPException(400,"Bugun bonus olgansiz")
    ex("update users set bal=bal+?,daily=? where id=?",(S("daily_bonus"),now(),uid));return {"ok":True}

def oembed_sync(link,plat):
    try:
        if plat=="yt":
            try:
                d=json.load(urllib.request.urlopen("https://www.youtube.com/oembed?format=json&url="+urllib.parse.quote(link,safe=""),timeout=8))
                return d.get("title","")+" | "+d.get("author_name","")
            except Exception:
                m=re.search(r"youtube\.com/(@[\w.\-]+)",link);return m.group(1) if m else ""
        m=re.search(r"instagram\.com/(?!p/|reel/|reels/)([A-Za-z0-9_.]+)",link);return m.group(1) if m else ""
    except Exception:return ""

def tg_parse(link):
    m=re.search(r"(?:t\.me/|^@)([A-Za-z][A-Za-z0-9_]{3,})(?:/(\d+))?",link.strip())
    return (m.group(1),m.group(2)) if m else (None,None)

class OrderIn(BaseModel):
    type:str
    link:str
    qty:int

@app.post("/api/order")
async def order(b:OrderIn,uid=Depends(ok)):
    P=S("price")
    if b.type not in P or not 1<=b.qty<=10000:raise HTTPException(400,"Noto'g'ri ma'lumot")
    link=b.link.strip();plat=b.type[:2];title="";chat=""
    if banned_text(link):raise HTTPException(400,"18+ va axloqqa zid kontent taqiqlangan")
    if plat=="tg":
        name,mid=tg_parse(link)
        if not name:raise HTTPException(400,"Faqat ochiq kanal havolasi: t.me/kanal")
        chat="@"+name;desc=""
        if b.type=="tg_sub":link=f"https://t.me/{name}"
        else:
            if not mid:raise HTTPException(400,"Post havolasi kerak: t.me/kanal/123")
            link=f"https://t.me/{name}/{mid}"
        c=None
        try:
            c=await bot.get_chat(chat);title=c.title or "";desc=c.description or ""
        except Exception:pass
        if b.type=="tg_sub":
            if not c:raise HTTPException(400,"Kanal topilmadi")
            try:m=await bot.get_chat_member(chat,BOT_ID)
            except Exception:raise HTTPException(400,"Botni kanalga admin qiling")
            if m.status not in("administrator","creator"):raise HTTPException(400,"Botni kanalga admin qiling")
        if banned_text(title+" "+desc+" "+name):raise HTTPException(400,"18+ kontent taqiqlangan")
    else:
        if (plat=="yt" and "youtu" not in link) or (plat=="ig" and "instagram.com" not in link):raise HTTPException(400,"Havola noto'g'ri")
        title=await asyncio.to_thread(oembed_sync,link,plat)
        if banned_text(title):raise HTTPException(400,"18+ kontent taqiqlangan")
    unit=P[b.type];cost=round(unit*b.qty,2)
    if ex("update users set bal=bal-? where id=? and bal>=?",(cost,uid,cost)).rowcount==0:raise HTTPException(400,"Mablag' yetarli emas")
    ex("insert into orders(uid,type,link,chat,title,qty,rem,reward,unit,status,created) values(?,?,?,?,?,?,?,?,?,?,?)",
       (uid,b.type,link,chat,title,b.qty,b.qty,round(unit*S("share"),2),unit,"on",now()))
    return {"ok":True}

def cancel(oid,uid,admin=False):
    o=q1("select * from orders where id=?",(oid,))
    if not o or o["status"]!="on" or (o["uid"]!=uid and not admin):raise HTTPException(400,"Topilmadi")
    ex("update orders set status='del' where id=?",(oid,))
    ex("update users set bal=bal+? where id=?",(o["rem"]*o["unit"],o["uid"]))

@app.post("/api/order_cancel/{oid}")
async def oc(oid:int,uid=Depends(ok)):
    cancel(oid,uid);return {"ok":True}

@app.get("/api/myorders")
async def myorders(uid=Depends(ok)):
    return [dict(r) for r in qa("select * from orders where uid=? and status='on' order by id desc",(uid,))]

@app.get("/api/tasks")
async def tasks(uid=Depends(ok)):
    return [dict(r) for r in qa("select id,type,link,title,rem,reward from orders where status='on' and rem>0 and uid!=? and id not in (select oid from dones where uid=?) order by id desc limit 100",(uid,uid))]

@app.post("/api/claim/{oid}")
async def claim(oid:int,uid=Depends(ok)):
    o=get_open(oid,uid)
    if o["type"]!="tg_sub":raise HTTPException(400,"Skrinshot yuboring")
    try:m=await bot.get_chat_member(o["chat"],uid)
    except Exception:raise HTTPException(400,"Tekshirib bo'lmadi")
    if m.status in("left","kicked"):raise HTTPException(400,"Obuna bo'lmadingiz")
    pay(uid,o);return {"ok":True,"reward":o["reward"]}

def dhash(im):
    p=list(im.convert("L").resize((17,16)).tobytes());v=0
    for y in range(16):
        for x in range(16):v=(v<<1)|(1 if p[y*17+x]>p[y*17+x+1] else 0)
    return v

ACT={"tg_react":"the user put an emoji reaction on this Telegram channel post; their reaction must be visibly highlighted/selected",
"tg_star":"the user put a paid Star (star icon) reaction on this Telegram post",
"yt_sub":"the user is subscribed to this YouTube channel (button shows 'Subscribed' / 'Obuna bo'lingan')",
"yt_like":"the user liked this YouTube video (like button highlighted/filled)",
"yt_comment":"the user posted a comment on this YouTube video and it is visible",
"ig_sub":"the user follows this Instagram account (button shows 'Following' / 'Obuna bo'lingan')",
"ig_like":"the user liked this Instagram post (heart is red/filled)",
"ig_comment":"the user posted a comment on this Instagram post and it is visible"}
LAST={}

async def ai_check(im,o):
    im.thumbnail((1280,1280));b=io.BytesIO();im.save(b,"JPEG",quality=80)
    local=(datetime.now(timezone.utc)+timedelta(hours=TZ)).strftime("%H:%M")
    prompt=(f"You verify a phone screenshot for a social task. Required action: {ACT[o['type']]}. "
    f"Expected target name/title: '{o['title'] or 'unknown'}' (link: {o['link']}). Current local time: {local}. "
    "Return ONLY a JSON object with keys: platform_ok (bool, correct app/platform), target_ok (bool, the expected account/channel/video/post is shown; true if target unknown but plausible), "
    "action_done (bool), clock (the phone status-bar clock as HH:MM, 24h if possible, or null if not visible), adult (bool, any sexual/nude content), "
    "edited (bool, looks edited/fake/old), reason (very short, in Uzbek).")
    r=await ai.messages.create(model=MODEL,max_tokens=300,messages=[{"role":"user","content":[
      {"type":"image","source":{"type":"base64","media_type":"image/jpeg","data":base64.b64encode(b.getvalue()).decode()}},
      {"type":"text","text":prompt}]}])
    j=json.loads(re.search(r"\{.*\}",r.content[0].text,re.S).group(0))
    if j.get("adult"):return False,"18+ kontent taqiqlangan"
    if not(j.get("platform_ok") and j.get("target_ok") and j.get("action_done")) or j.get("edited"):
        return False,j.get("reason") or "Skrinshot talabga mos emas"
    c=j.get("clock")
    if not c or not re.match(r"^\d{1,2}:\d{2}$",str(c)):return False,"Telefon soati ko'rinadigan to'liq ekran skrinshot yuboring"
    h,mi=map(int,str(c).split(":"));a=h*60+mi;n=int(local[:2])*60+int(local[3:])
    def df(x,y):
        d=abs(x-y);return min(d,1440-d)
    if min(df(a,n),df((a+720)%1440,n))>10:return False,"Skrinshot vaqti eski. Hozir olingan skrinshot yuboring"
    return True,""

@app.post("/api/shot/{oid}")
async def shot(oid:int,file:UploadFile=File(...),uid=Depends(ok)):
    o=get_open(oid,uid)
    if o["type"]=="tg_sub":raise HTTPException(400,"Bu vazifa avtomatik tekshiriladi")
    if time.time()-LAST.get(uid,0)<4:raise HTTPException(429,"Sekinroq yuboring")
    LAST[uid]=time.time()
    raw=await file.read()
    if len(raw)>8_000_000:raise HTTPException(400,"Rasm juda katta")
    try:im=Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception:raise HTTPException(400,"Bu rasm emas")
    h=dhash(im)
    for r in qa("select hash from shots"):
        if bin(int(r["hash"])^h).count("1")<=10:raise HTTPException(400,"Bu skrinshot oldin yuborilgan")
    try:good,why=await ai_check(im,o)
    except Exception:raise HTTPException(503,"Tekshiruv vaqtincha ishlamayapti, keyinroq urinib ko'ring")
    if not good:raise HTTPException(400,why)
    try:ex("insert into shots values(?,?,?)",(str(h),uid,now()))
    except sqlite3.IntegrityError:raise HTTPException(400,"Bu skrinshot oldin yuborilgan")
    pay(uid,o);return {"ok":True,"reward":o["reward"]}

@app.post("/api/deposit")
async def deposit(som:int=Form(...),file:UploadFile=File(...),uid=Depends(ok)):
    if som<S("min_dep"):raise HTTPException(400,f"Minimum: {S('min_dep')} so'm")
    raw=await file.read();rub=round(som/100000*S("rate"),2)
    did=ex("insert into deps(uid,som,rub,status,ts) values(?,?,?,?,?)",(uid,som,rub,"wait",now())).lastrowid
    kb=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Tasdiqlash",callback_data=f"dp:{did}:1"),InlineKeyboardButton(text="❌ Rad",callback_data=f"dp:{did}:0")]])
    for a in ADMINS:
        try:await bot.send_photo(a,BufferedInputFile(raw,"check.jpg"),caption=f"💳 Depozit #{did}\nUser: {uid}\n{som} so'm → {rub} ₽",reply_markup=kb)
        except Exception:pass
    return {"ok":True}

@app.get("/api/admin/stats")
async def stats(a=Depends(adm)):
    return {"users":q1("select count(*) c from users")["c"],"dep":q1("select coalesce(sum(dep),0) c from users")["c"],
    "orders":q1("select count(*) c from orders where status='on'")["c"],"dones":q1("select count(*) c from dones")["c"]}
@app.get("/api/admin/settings")
async def gs(a=Depends(adm)):
    return {k:S(k) for k in DEF if k!="contest_start"}
@app.post("/api/admin/settings")
async def ss(b:dict,a=Depends(adm)):
    for k,v in b.items():
        if k not in DEF or k=="contest_start":continue
        num=isinstance(DEF[k],(int,float)) and isinstance(v,(int,float))
        if num or type(v)==type(DEF[k]):ex("insert or replace into settings values(?,?)",(k,json.dumps(v)))
    return {"ok":True}
@app.post("/api/admin/contest_reset")
async def cr(a=Depends(adm)):
    ex("insert or replace into settings values('contest_start',?)",(json.dumps(now()),));return {"ok":True}
@app.post("/api/admin/user")
async def admin_user(b:dict,a=Depends(adm)):
    i=int(b["id"]);act=b["action"]
    if not q1("select 1 from users where id=?",(i,)):raise HTTPException(400,"Foydalanuvchi topilmadi")
    if act=="give":ex("update users set bal=bal+? where id=?",(float(b.get("amount",0)),i))
    elif act=="zero":ex("update users set bal=0 where id=?",(i,))
    elif act=="ban":ex("update users set banned=1 where id=?",(i,))
    elif act=="unban":ex("update users set banned=0 where id=?",(i,))
    return {"ok":True,"bal":q1("select bal from users where id=?",(i,))["bal"]}
async def bc(text):
    for r in qa("select id from users"):
        try:await bot.send_message(r["id"],text)
        except Exception:pass
        await asyncio.sleep(0.05)
@app.post("/api/admin/broadcast")
async def bcast(b:dict,a=Depends(adm)):
    asyncio.create_task(bc(str(b["text"])));return {"ok":True}
@app.get("/api/admin/orders")
async def aorders(a=Depends(adm)):
    return [dict(r) for r in qa("select id,uid,type,link,title,rem,qty from orders where status='on' order by id desc limit 50")]
@app.post("/api/admin/order_del")
async def odel(b:dict,a=Depends(adm)):
    cancel(int(b["id"]),0,True);return {"ok":True}
