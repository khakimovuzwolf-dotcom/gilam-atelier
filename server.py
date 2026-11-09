"""Gilam Atelier: dependency-free storefront + admin, SQLite, local image uploads.
Start with ADMIN_PASSWORD and SESSION_SECRET in environment. No online payments.
"""
import os, json, re, sqlite3, secrets, hmac, hashlib, time, base64, binascii, mimetypes
from datetime import datetime, timezone
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlsplit, parse_qs
from http.cookies import SimpleCookie
from threading import Lock

ROOT=Path(__file__).resolve().parent
DB=Path(os.environ.get('DATABASE_PATH', ROOT/'store.db'))
UPLOADS=ROOT/'uploads';UPLOADS.mkdir(parents=True,exist_ok=True)
ADMIN_PASSWORD=os.environ.get('ADMIN_PASSWORD','')
SECRET=os.environ.get('SESSION_SECRET','')
LOCK=Lock()
STATUS=('new','confirmed','shipped','completed','cancelled')
TYPES=('modern','classic','national')

def db():
 c=sqlite3.connect(DB,timeout=15)
 c.row_factory=sqlite3.Row
 c.execute('PRAGMA busy_timeout=15000')
 c.execute('PRAGMA foreign_keys=ON')
 return c

def cols(c,t):return {r[1] for r in c.execute('PRAGMA table_info('+t+')')}
def add(c,t,name,sql):
 if name not in cols(c,t):c.execute(f'ALTER TABLE {t} ADD COLUMN {name} {sql}')
def init():
 DB.parent.mkdir(parents=True,exist_ok=True)
 with db() as c:
  c.execute('PRAGMA journal_mode=WAL')
  c.executescript('''
  CREATE TABLE IF NOT EXISTS products(id INTEGER PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL,
    size TEXT NOT NULL,price INTEGER NOT NULL,old INTEGER,bg TEXT,base TEXT,accent TEXT,badge TEXT,
    stock INTEGER NOT NULL DEFAULT 0,active INTEGER NOT NULL DEFAULT 1);
  CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY,created_at TEXT NOT NULL DEFAULT (datetime('now')),
    name TEXT NOT NULL,phone TEXT NOT NULL,address TEXT NOT NULL,total INTEGER NOT NULL,status TEXT NOT NULL DEFAULT 'new');
  CREATE TABLE IF NOT EXISTS order_items(order_id TEXT NOT NULL,product_id INTEGER NOT NULL,name TEXT NOT NULL,price INTEGER NOT NULL,qty INTEGER NOT NULL);
  CREATE TABLE IF NOT EXISTS subscribers(email TEXT PRIMARY KEY,created_at TEXT NOT NULL DEFAULT (datetime('now')));
  CREATE TABLE IF NOT EXISTS variants(id INTEGER PRIMARY KEY AUTOINCREMENT,product_id INTEGER NOT NULL,
    label TEXT NOT NULL,price INTEGER NOT NULL,old INTEGER,stock INTEGER NOT NULL DEFAULT 0,active INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY(product_id) REFERENCES products(id));
  CREATE INDEX IF NOT EXISTS ix_variant_product ON variants(product_id);
  ''')
  for name,sql in [('description',"TEXT NOT NULL DEFAULT ''"),('material',"TEXT NOT NULL DEFAULT ''"),('image',"TEXT NOT NULL DEFAULT ''"),('archived','INTEGER NOT NULL DEFAULT 0'),('images',"TEXT NOT NULL DEFAULT '[]'"),('origin',"TEXT NOT NULL DEFAULT ''"),('pile',"TEXT NOT NULL DEFAULT ''"),('care',"TEXT NOT NULL DEFAULT ''")]:add(c,'products',name,sql)
  for name,sql in [('variant_id','INTEGER'),('size',"TEXT NOT NULL DEFAULT ''"),('image',"TEXT NOT NULL DEFAULT ''"),('material',"TEXT NOT NULL DEFAULT ''")]:add(c,'order_items',name,sql)
  if c.execute('SELECT count(*) FROM products').fetchone()[0]==0:
   for p in json.loads((ROOT/'seed_products.json').read_text(encoding='utf-8')):
    c.execute('''INSERT INTO products(id,name,type,size,price,old,bg,base,accent,badge,stock,active,material,description)
       VALUES(?,?,?,?,?,?,?,?,?,?,?,1,?,?)''',(p['id'],p['name'],p['type'],p['size'],p['price'],p.get('old'),p.get('bg'),p.get('base'),p.get('accent'),p.get('badge',''),p['stock'],'Tarkib ma’lumoti kiritilmagan','Ushbu mahsulot namunaviy katalogga tegishli. Haqiqiy xususiyatlarni admin paneldan to‘ldiring.'))
  # Migrate previous versions without losing order history.
  for p in c.execute('SELECT * FROM products').fetchall():
   if not c.execute('SELECT 1 FROM variants WHERE product_id=?',(p['id'],)).fetchone():
    c.execute('INSERT INTO variants(product_id,label,price,old,stock,active) VALUES(?,?,?,?,?,1)',(p['id'],p['size'],p['price'],p['old'],p['stock']))
   if p['image'] and p['images']=='[]':c.execute('UPDATE products SET images=? WHERE id=?',(json.dumps([p['image']]),p['id']))
  for i in c.execute('SELECT rowid,product_id FROM order_items WHERE variant_id IS NULL').fetchall():
   v=c.execute('SELECT id,label FROM variants WHERE product_id=? ORDER BY id LIMIT 1',(i['product_id'],)).fetchone()
   if v:c.execute('UPDATE order_items SET variant_id=?,size=? WHERE rowid=?',(v['id'],v['label'],i['rowid']))

def variants(c,pid,private=False):
 q='SELECT id,product_id,label,price,old,stock,active FROM variants WHERE product_id=?'
 if not private:q+=' AND active=1'
 return [dict(v) for v in c.execute(q+' ORDER BY id',(pid,))]

def products(c,private=False):
 q='SELECT * FROM products'
 if not private:q+=' WHERE active=1 AND archived=0'
 result=[]
 for r in c.execute(q+' ORDER BY id DESC').fetchall():
  p=dict(r)
  try:p['images']=json.loads(p['images'] or '[]')
  except (TypeError,ValueError):p['images']=[]
  if p['image'] and p['image'] not in p['images']:p['images'].insert(0,p['image'])
  p['variants']=variants(c,p['id'],private)
  if not private and not p['variants']:continue
  result.append(p)
 return result

def sign(raw):return hmac.new(SECRET.encode(),raw.encode(),hashlib.sha256).hexdigest()
def authorized(headers):
 if not (ADMIN_PASSWORD and SECRET):return False
 try:
  ck=SimpleCookie();ck.load(headers.get('Cookie',''));v=ck['gilam_admin'].value
  val,sig=v.rsplit('.',1);ts,pwd=val.split(':')
  return hmac.compare_digest(sig,sign(val)) and hmac.compare_digest(pwd,hashlib.sha256(ADMIN_PASSWORD.encode()).hexdigest()[:16]) and 0<=time.time()-int(ts)<28800
 except (KeyError,TypeError,ValueError):return False

def string(d,k,minimum=0,maximum=200,required=True):
 v=d.get(k,'')
 if not isinstance(v,str) or not minimum<=len(v.strip())<=maximum:raise ValueError(f'{k}: noto‘g‘ri qiymat')
 return v.strip()
def integer(v,field,minimum=0,maximum=1000000000):
 if type(v) is not int or not minimum<=v<=maximum:raise ValueError(field+': noto‘g‘ri son')
 return v

def image_url(url):
 return isinstance(url,str) and bool(re.fullmatch(r'/uploads/[0-9a-f]{32}\.(jpg|png|webp)',url)) and (UPLOADS/url.rsplit('/',1)[1]).is_file()

class Handler(BaseHTTPRequestHandler):
 def log_message(self,fmt,*a):pass
 def send(self,code,body,ctype='application/json; charset=utf-8',headers=None):
  if isinstance(body,(dict,list)):body=json.dumps(body,ensure_ascii=False).encode('utf-8')
  if isinstance(body,str):body=body.encode('utf-8')
  self.send_response(code);self.send_header('Content-Type',ctype)
  self.send_header('Content-Length',str(len(body)))
  for k,v in {'X-Content-Type-Options':'nosniff','X-Frame-Options':'DENY','Referrer-Policy':'same-origin','Cache-Control':'no-store','Content-Security-Policy':"default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'"}.items():self.send_header(k,v)
  for k,v in (headers or {}).items():self.send_header(k,v)
  self.end_headers();self.wfile.write(body)
 def body(self):
  length=int(self.headers.get('Content-Length','0'))
  if not 0<length<=6000000:raise ValueError('So‘rov hajmi noto‘g‘ri')
  value=json.loads(self.rfile.read(length))
  if not isinstance(value,dict):raise ValueError('JSON obyekt talab qilinadi')
  return value
 def do_GET(self):
  path=urlsplit(self.path).path
  if path in ('/','/index.html'):return self.send(200,(ROOT/'index.html').read_bytes(),'text/html; charset=utf-8')
  if path in ('/admin','/admin.html'):return self.send(200,(ROOT/'admin.html').read_bytes(),'text/html; charset=utf-8')
  if path=='/health':return self.send(200,{'ok':True})
  if path.startswith('/uploads/'):
   name=path[len('/uploads/'):]
   if not re.fullmatch('[0-9a-f]{32}\\.(jpg|png|webp)',name):return self.send(404,{'error':'Rasm topilmadi'})
   file=UPLOADS/name
   if not file.is_file():return self.send(404,{'error':'Rasm topilmadi'})
   return self.send(200,file.read_bytes(),{'jpg':'image/jpeg','png':'image/png','webp':'image/webp'}[name.rsplit('.',1)[1]])
  if path=='/api/products':
   with db() as c:return self.send(200,products(c))
  if path.startswith('/api/admin/'):
   if not authorized(self.headers):return self.send(403,{'error':'Avval tizimga kiring'})
   with db() as c:
    if path=='/api/admin/products':return self.send(200,products(c,True))
    if path=='/api/admin/orders':
     rows=[dict(r) for r in c.execute('SELECT * FROM orders ORDER BY created_at DESC,id DESC LIMIT 1000')]
     for o in rows:o['items']=[dict(r) for r in c.execute('SELECT product_id,variant_id,name,size,image,material,price,qty FROM order_items WHERE order_id=?',(o['id'],))]
     return self.send(200,rows)
  return self.send(404,{'error':'Sahifa topilmadi'})
 def do_POST(self):
  path=urlsplit(self.path).path
  try:
   if path=='/api/orders':return self.order(self.body())
   if path=='/api/admin/login':return self.login(self.body())
   if path=='/api/admin/logout':return self.send(200,{'ok':True},headers={'Set-Cookie':'gilam_admin=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0'})
   if path.startswith('/api/admin/'):
    if not authorized(self.headers):return self.send(403,{'error':'Admin huquqi talab qilinadi'})
    data=self.body()
    if path=='/api/admin/product/save':return self.save_product(data)
    if path=='/api/admin/product/archive':return self.archive(data)
    if path=='/api/admin/image/upload':return self.upload(data)
    if path=='/api/admin/status':return self.change_status(data)
   return self.send(404,{'error':'Endpoint topilmadi'})
  except (ValueError,TypeError,KeyError,json.JSONDecodeError) as err:self.send(400,{'error':str(err)})
  except sqlite3.Error as err:
   print('Database error:',repr(err),flush=True);self.send(500,{'error':'Baza xatoligi. CMD oynasini tekshiring.'})
 def login(self,d):
  if not ADMIN_PASSWORD or not SECRET:return self.send(503,{'error':'ADMIN_PASSWORD va SESSION_SECRET o‘rnatilmagan'})
  pwd=d.get('password')
  if not isinstance(pwd,str) or not hmac.compare_digest(pwd,ADMIN_PASSWORD):return self.send(401,{'error':'Parol noto‘g‘ri'})
  raw=str(int(time.time()))+':'+hashlib.sha256(ADMIN_PASSWORD.encode()).hexdigest()[:16]
  cookie=raw+'.'+sign(raw)
  secure='; Secure' if os.getenv('COOKIE_SECURE')=='1' else ''
  self.send(200,{'ok':True},headers={'Set-Cookie':f'gilam_admin={cookie}; HttpOnly; SameSite=Strict; Path=/; Max-Age=28800{secure}'})
 def upload(self,d):
  raw=d.get('data','')
  if not isinstance(raw,str) or len(raw)>5700000:raise ValueError('Rasm 4 MB dan kichik bo‘lsin')
  m=re.fullmatch(r'data:image/(jpeg|png|webp);base64,([a-zA-Z0-9+/=]+)',raw)
  if not m:raise ValueError('Faqat JPG, PNG va WebP rasm yuklang')
  try:blob=base64.b64decode(m[2],validate=True)
  except binascii.Error:raise ValueError('Rasm kodida xatolik')
  if not 0<len(blob)<=4*1024*1024:raise ValueError('Rasm hajmi 4 MB dan oshmasin')
  kind=m[1]
  if not {'jpeg':blob.startswith(b'\xff\xd8\xff'),'png':blob.startswith(b'\x89PNG\r\n\x1a\n'),'webp':blob.startswith(b'RIFF') and blob[8:12]==b'WEBP'}[kind]:raise ValueError('Rasm turi mos kelmadi')
  file=secrets.token_hex(16)+'.'+('jpg' if kind=='jpeg' else kind)
  (UPLOADS/file).write_bytes(blob)
  self.send(201,{'url':'/uploads/'+file})
 def save_product(self,d):
  name=string(d,'name',2,120);typ=string(d,'type',2,20)
  if typ not in TYPES:raise ValueError('Kategoriya noto‘g‘ri')
  material=string(d,'material',2,250);description=string(d,'description',10,3000)
  origin=string(d,'origin',0,100);pile=string(d,'pile',0,100);care=string(d,'care',0,600)
  badge=string(d,'badge',0,40)
  active=d.get('active',True)
  if type(active) is not bool:raise ValueError('Ko‘rinish qiymati noto‘g‘ri')
  images=d.get('images',[])
  if not isinstance(images,list) or not 1<=len(images)<=8 or any(not image_url(u) for u in images) or len(set(images))!=len(images):raise ValueError('1–8 ta haqiqiy rasm yuklang')
  vs=d.get('variants')
  if not isinstance(vs,list) or not 1<=len(vs)<=20:raise ValueError('Kamida bitta o‘lcham/narx kiriting')
  normalized=[];seen=set()
  for v in vs:
   if not isinstance(v,dict):raise ValueError('O‘lchamlar noto‘g‘ri')
   label=string(v,'label',3,80)
   if label.lower() in seen:raise ValueError('Bir xil o‘lcham takrorlandi')
   seen.add(label.lower());price=integer(v.get('price'),'Narx',1)
   old=v.get('old')
   if old in ('',None):old=None
   else:old=integer(old,'Eski narx',1)
   if old is not None and old<=price:raise ValueError('Eski narx yangi narxdan katta bo‘lishi kerak')
   stock=integer(v.get('stock'),'Qoldiq',0,100000)
   vid=v.get('id')
   if vid is not None:integer(vid,'Variant ID',1)
   normalized.append((vid,label,price,old,stock))
  pid=d.get('id');integer(pid,'Mahsulot ID',1) if pid is not None else None
  with LOCK:
   with db() as c:
    c.execute('BEGIN IMMEDIATE')
    if pid is None:
     cur=c.execute('''INSERT INTO products(name,type,size,price,old,bg,base,accent,badge,stock,active,description,material,image,images,origin,pile,care)
     VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(name,typ,normalized[0][1],normalized[0][2],normalized[0][3],'#eee9e1','#a77b61','#e9d5bb',badge,normalized[0][4],int(active),description,material,images[0],json.dumps(images),origin,pile,care))
     pid=cur.lastrowid
    else:
     if not c.execute('SELECT 1 FROM products WHERE id=?',(pid,)).fetchone():raise ValueError('Mahsulot topilmadi')
     c.execute('''UPDATE products SET name=?,type=?,size=?,price=?,old=?,badge=?,stock=?,active=?,description=?,material=?,image=?,images=?,origin=?,pile=?,care=? WHERE id=?''',(name,typ,normalized[0][1],normalized[0][2],normalized[0][3],badge,normalized[0][4],int(active),description,material,images[0],json.dumps(images),origin,pile,care,pid))
    prev={r[0] for r in c.execute('SELECT id FROM variants WHERE product_id=?',(pid,))}
    used=set()
    for vid,label,price,old,stock in normalized:
     if vid is not None:
      if vid not in prev:raise ValueError('Variant boshqa mahsulotga tegishli yoki yo‘q')
      c.execute('UPDATE variants SET label=?,price=?,old=?,stock=?,active=1 WHERE id=? AND product_id=?',(label,price,old,stock,vid,pid));used.add(vid)
     else:
      nv=c.execute('INSERT INTO variants(product_id,label,price,old,stock,active) VALUES(?,?,?,?,?,1)',(pid,label,price,old,stock));used.add(nv.lastrowid)
    # Historical order items continue to reference removed variants; do not delete them.
    for vid in prev-used:c.execute('UPDATE variants SET active=0,stock=0 WHERE id=?',(vid,))
  self.send(200,{'ok':True,'id':pid})
 def archive(self,d):
  pid=integer(d.get('id'),'ID',1);archive=d.get('archived')
  if type(archive) is not bool:raise ValueError('Holat noto‘g‘ri')
  with db() as c:
   r=c.execute('UPDATE products SET archived=?,active=CASE WHEN ? THEN 0 ELSE 1 END WHERE id=?',(int(archive),int(archive),pid))
   if not r.rowcount:raise ValueError('Mahsulot topilmadi')
  self.send(200,{'ok':True})
 def order(self,d):
  name=string(d,'name',2,90);address=string(d,'address',8,300)
  phone=string(d,'phone',13,13)
  if not re.fullmatch(r'\+998\d{9}',phone):raise ValueError('Telefon raqam +998XXXXXXXXX shaklida bo‘lsin')
  note=string(d,'note',0,500)
  items=d.get('items')
  if not isinstance(items,list) or not 1<=len(items)<=30:raise ValueError('Savatcha bo‘sh yoki noto‘g‘ri')
  counts={}
  for item in items:
   if not isinstance(item,dict):raise ValueError('Mahsulot noto‘g‘ri')
   vid=integer(item.get('variant_id'),'O‘lcham ID',1)
   qty=integer(item.get('qty'),'Soni',1,99)
   counts[vid]=counts.get(vid,0)+qty
   if counts[vid]>99:raise ValueError('Miqdor chegaradan oshdi')
  with LOCK:
   with db() as c:
    c.execute('BEGIN IMMEDIATE')
    chosen=[];total=0
    for vid,qty in counts.items():
     r=c.execute('''SELECT v.*,p.name,p.material,p.image,p.images,p.archived,p.active AS product_active
                   FROM variants v JOIN products p ON p.id=v.product_id WHERE v.id=?''',(vid,)).fetchone()
     if not r or not r['active'] or not r['product_active'] or r['archived']:raise ValueError('Tanlangan gilam sotuvda mavjud emas')
     if r['stock']<qty:raise ValueError(f"{r['name']}, {r['label']}: faqat {r['stock']} dona qolgan")
     total+=r['price']*qty;chosen.append((r,qty))
    oid='GL-'+secrets.token_hex(5).upper()
    c.execute('INSERT INTO orders(id,name,phone,address,total) VALUES(?,?,?,?,?)',(oid,name,phone,address,total))
    # Add optional note for installation with existing databases.
    if 'note' not in cols(c,'orders'):add(c,'orders','note',"TEXT NOT NULL DEFAULT ''")
    c.execute('UPDATE orders SET note=? WHERE id=?',(note,oid))
    for r,qty in chosen:
     c.execute('''INSERT INTO order_items(order_id,product_id,variant_id,name,size,image,material,price,qty)
                VALUES(?,?,?,?,?,?,?,?,?)''',(oid,r['product_id'],r['id'],r['name'],r['label'],r['image'],r['material'],r['price'],qty))
     c.execute('UPDATE variants SET stock=stock-? WHERE id=?',(qty,r['id']))
     first=c.execute('SELECT id FROM variants WHERE product_id=? ORDER BY id LIMIT 1',(r['product_id'],)).fetchone()
     if first and first['id']==r['id']:c.execute('UPDATE products SET stock=stock-? WHERE id=?',(qty,r['product_id']))
  self.send(201,{'ok':True,'order_id':oid,'total':total})
 def change_status(self,d):
  oid=string(d,'id',5,40);status=string(d,'status',3,20)
  if status not in STATUS:raise ValueError('Buyurtma holati noto‘g‘ri')
  with LOCK:
   with db() as c:
    c.execute('BEGIN IMMEDIATE');r=c.execute('SELECT status FROM orders WHERE id=?',(oid,)).fetchone()
    if not r:raise ValueError('Buyurtma topilmadi')
    if r['status']=='cancelled' and status!='cancelled':raise ValueError('Bekor qilingan buyurtmani qayta ochib bo‘lmaydi')
    if status=='cancelled' and r['status']!='cancelled':
     for item in c.execute('SELECT product_id,variant_id,qty FROM order_items WHERE order_id=?',(oid,)).fetchall():
      c.execute('UPDATE variants SET stock=stock+? WHERE id=?',(item['qty'],item['variant_id']))
      first=c.execute('SELECT id FROM variants WHERE product_id=? ORDER BY id LIMIT 1',(item['product_id'],)).fetchone()
      if first and first['id']==item['variant_id']:c.execute('UPDATE products SET stock=stock+? WHERE id=?',(item['qty'],item['product_id']))
    c.execute('UPDATE orders SET status=? WHERE id=?',(status,oid))
  self.send(200,{'ok':True})

if __name__=='__main__':
 if not ADMIN_PASSWORD or not SECRET:
  raise SystemExit('XATO: ADMIN_PASSWORD va SESSION_SECRET muhit o‘zgaruvchilarini belgilang. START-WINDOWS.cmd yo‘riqnomasi README da.')
 init();port=int(os.environ.get('PORT','8000'))
 print('GILAM ATELIER ishlayapti: http://127.0.0.1:'+str(port),flush=True)
 ThreadingHTTPServer((os.environ.get('HOST', '0.0.0.0'), port), Handler).serve_forever()
