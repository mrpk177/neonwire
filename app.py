"""
NEONWIRE — single-file backend.

Everything the multi-file version had (real database, real admin-only
auth, real translation pipeline) lives in one file here on purpose: this
is the minimal version, made for getting a working deployment onto
GitHub from a phone with the least possible number of files to create.
The clustering/verification engine and live news ingestion from the
fuller version aren't included here — this version serves pre-written
demo stories plus whatever you add by hand through the admin API. Once
this is live and working, the fuller version (with real multi-source
ingestion) is a natural next step, not a rewrite.
"""
import os
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, List

import requests
from fastapi import FastAPI, Depends, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import (create_engine, Column, Integer, String, Float,
                         Boolean, ForeignKey, DateTime, Text, or_)
from sqlalchemy.orm import sessionmaker, declarative_base, relationship, Session
from jose import JWTError, jwt
from passlib.context import CryptContext

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("neonwire")

# ============================== DATABASE ==============================

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./neonwire.db")
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ============================== MODELS ==============================

class AdminUser(Base):
    __tablename__ = "admin_users"
    id = Column(Integer, primary_key=True)
    username = Column(String(64), unique=True, nullable=False)
    hashed_password = Column(String(256), nullable=False)


class Story(Base):
    __tablename__ = "stories"
    id = Column(Integer, primary_key=True)
    title = Column(String(400), nullable=False)
    summary = Column(Text, nullable=False)
    category = Column(String(64), index=True, nullable=False)
    location_name = Column(String(200), index=True)
    pincode = Column(String(20), index=True)
    score = Column(Float, nullable=False, default=0.0)
    official_confirmed = Column(Boolean, default=False)
    published = Column(Boolean, default=False, index=True)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    sources = relationship("SourceRecord", back_populates="story", cascade="all, delete-orphan")
    translations = relationship("StoryTranslation", back_populates="story", cascade="all, delete-orphan")


class StoryTranslation(Base):
    __tablename__ = "story_translations"
    id = Column(Integer, primary_key=True)
    story_id = Column(Integer, ForeignKey("stories.id"), nullable=False)
    lang = Column(String(8), nullable=False, index=True)
    title = Column(String(400), nullable=False)
    summary = Column(Text, nullable=False)
    story = relationship("Story", back_populates="translations")


class SourceRecord(Base):
    __tablename__ = "source_records"
    id = Column(Integer, primary_key=True)
    story_id = Column(Integer, ForeignKey("stories.id"), nullable=False)
    source_name = Column(String(200), nullable=False)
    is_official = Column(Boolean, default=False)
    agrees = Column(Boolean, default=True)
    story = relationship("Story", back_populates="sources")


Base.metadata.create_all(bind=engine)

# ============================== AUTH ==============================
# No public signup route exists anywhere in this file — the only way an
# admin account is created is create_admin_from_env() below, run once at
# startup from ADMIN_USERNAME/ADMIN_PASSWORD. This is the real mechanism
# behind "only I can edit this site."

SECRET_KEY = os.getenv("NEONWIRE_SECRET_KEY", "dev-only-insecure-change-me")
ALGORITHM = "HS256"
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login", auto_error=False)


def create_access_token(username: str) -> str:
    expire = datetime.now(timezone.utc) + timedelta(hours=12)
    return jwt.encode({"sub": username, "exp": expire}, SECRET_KEY, algorithm=ALGORITHM)


def require_admin(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)) -> AdminUser:
    err = HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authorized")
    if not token:
        raise err
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username = payload.get("sub")
        if not username:
            raise err
    except JWTError:
        raise err
    user = db.query(AdminUser).filter(AdminUser.username == username).first()
    if not user:
        raise err
    return user


def create_admin_from_env():
    username = os.getenv("ADMIN_USERNAME", "").strip()
    password = os.getenv("ADMIN_PASSWORD", "")
    if not username or not password:
        logger.info("ADMIN_USERNAME/ADMIN_PASSWORD not set — skipping admin creation.")
        return
    db = SessionLocal()
    try:
        if db.query(AdminUser).filter(AdminUser.username == username).first():
            logger.info(f"Admin '{username}' already exists — leaving as is.")
            return
        db.add(AdminUser(username=username, hashed_password=pwd_context.hash(password)))
        db.commit()
        logger.info(f"Admin '{username}' created.")
    finally:
        db.close()


# ============================== TRANSLATION ==============================
# Pluggable: mock (default, no key needed, labeled placeholder text) | deepl | google

PROVIDER = os.getenv("TRANSLATION_PROVIDER", "mock").lower()
DEEPL_API_KEY = os.getenv("DEEPL_API_KEY", "")
GOOGLE_TRANSLATE_API_KEY = os.getenv("GOOGLE_TRANSLATE_API_KEY", "")


def translate_text(text: str, target_lang: str) -> str:
    if not text:
        return text
    try:
        if PROVIDER == "deepl" and DEEPL_API_KEY:
            r = requests.post("https://api-free.deepl.com/v2/translate",
                               data={"auth_key": DEEPL_API_KEY, "text": text,
                                     "target_lang": target_lang.upper()}, timeout=10)
            r.raise_for_status()
            return r.json()["translations"][0]["text"]
        if PROVIDER == "google" and GOOGLE_TRANSLATE_API_KEY:
            r = requests.post("https://translation.googleapis.com/language/translate/v2",
                               params={"key": GOOGLE_TRANSLATE_API_KEY},
                               data={"q": text, "target": target_lang, "format": "text"}, timeout=10)
            r.raise_for_status()
            return r.json()["data"]["translations"][0]["translatedText"]
    except Exception as e:
        logger.warning(f"Translation failed ({PROVIDER} -> {target_lang}): {e}. Using original text.")
        return text
    return f"[{target_lang}] {text}"  # mock provider (or unset key) fallback — clearly labeled, not real


def translate_story(title: str, summary: str, langs: List[str]) -> dict:
    return {lang: {"title": translate_text(title, lang), "summary": translate_text(summary, lang)} for lang in langs}


# ============================== DEMO SEED DATA ==============================

TARGET_LANGUAGES = ["es", "fr", "hi", "ar"]

DEMO_STORIES = [
    {"title": "Regional ceasefire talks resume after week-long pause",
     "summary": "Delegations returned to the negotiating table this morning, with mediators describing early signals as cautiously constructive. No formal agreement has been signed.",
     "category": "World", "location_name": "Geneva, Switzerland", "pincode": "1201",
     "score": 96.0, "official_confirmed": True,
     "sources": [{"source_name": "Reuters", "is_official": False}, {"source_name": "AP", "is_official": False},
                 {"source_name": "BBC", "is_official": False}, {"source_name": "Al Jazeera", "is_official": False},
                 {"source_name": "UN Press Office", "is_official": True}]},
    {"title": "National telecom regulator approves new spectrum rules",
     "summary": "The updated framework is expected to lower rollout costs for rural 5G coverage. Industry bodies broadly welcomed the move; two operators flagged pricing concerns.",
     "category": "Technology", "location_name": "New Delhi, India", "pincode": "110001",
     "score": 94.0, "official_confirmed": True,
     "sources": [{"source_name": "Ministry of Telecom (PIB)", "is_official": True},
                 {"source_name": "Times of India", "is_official": False},
                 {"source_name": "Hindustan Times", "is_official": False},
                 {"source_name": "Reuters India", "is_official": False}]},
    {"title": "Health ministry confirms updated seasonal vaccine guidance",
     "summary": "Updated guidance recommends earlier booster timing for high-risk groups. Confirmed directly against the ministry's published circular.",
     "category": "Health", "location_name": "Mumbai, India", "pincode": "400001",
     "score": 98.0, "official_confirmed": True,
     "sources": [{"source_name": "Ministry of Health & FW", "is_official": True},
                 {"source_name": "WHO Regional Office", "is_official": True},
                 {"source_name": "Hindustan Times", "is_official": False},
                 {"source_name": "CNN", "is_official": False}]},
    {"title": "Central bank holds interest rates, signals cautious outlook",
     "summary": "Policymakers cited mixed inflation data. Markets reacted with modest gains after the announcement.",
     "category": "Business", "location_name": "New York, USA", "pincode": "10001",
     "score": 95.0, "official_confirmed": True,
     "sources": [{"source_name": "Federal Reserve statement", "is_official": True},
                 {"source_name": "Bloomberg", "is_official": False},
                 {"source_name": "Reuters", "is_official": False},
                 {"source_name": "Wall Street Journal", "is_official": False}]},
    {"title": "Space agency confirms successful satellite deployment",
     "summary": "The satellite entered its planned orbit within expected parameters, according to a mission-control briefing.",
     "category": "Science", "location_name": "Bengaluru, India", "pincode": "560001",
     "score": 99.0, "official_confirmed": True,
     "sources": [{"source_name": "ISRO official release", "is_official": True},
                 {"source_name": "Reuters", "is_official": False}, {"source_name": "BBC", "is_official": False},
                 {"source_name": "Times of India", "is_official": False}]},
    {"title": "Transit authority opens new cross-harbour rail line",
     "summary": "The new line is expected to cut commute times by roughly a third for residents on the eastern waterfront.",
     "category": "World", "location_name": "Tokyo, Japan", "pincode": "100-0001",
     "score": 97.0, "official_confirmed": True,
     "sources": [{"source_name": "Ministry of Land, Infrastructure & Transport", "is_official": True},
                 {"source_name": "NHK", "is_official": False}, {"source_name": "Reuters", "is_official": False},
                 {"source_name": "Kyodo News", "is_official": False}]},
]


def seed_demo_data():
    if os.getenv("SEED_DEMO_DATA", "true").lower() != "true":
        return
    db = SessionLocal()
    try:
        if db.query(Story).first() is not None:
            logger.info("Database already has stories — skipping demo seed.")
            return
        logger.info(f"Seeding {len(DEMO_STORIES)} demo stories (translation provider: {PROVIDER})...")
        for item in DEMO_STORIES:
            story = Story(title=item["title"], summary=item["summary"], category=item["category"],
                          location_name=item["location_name"], pincode=item["pincode"],
                          score=item["score"], official_confirmed=item["official_confirmed"], published=True)
            for src in item["sources"]:
                story.sources.append(SourceRecord(source_name=src["source_name"],
                                                    is_official=src["is_official"], agrees=True))
            translated = translate_story(item["title"], item["summary"], TARGET_LANGUAGES)
            for lang, fields in translated.items():
                story.translations.append(StoryTranslation(lang=lang, title=fields["title"], summary=fields["summary"]))
            db.add(story)
        db.commit()
        logger.info("Demo seed complete.")
    finally:
        db.close()


# ============================== SCHEMAS ==============================

class SourceOut(BaseModel):
    source_name: str
    is_official: bool
    agrees: bool
    class Config:
        from_attributes = True


class TranslationOut(BaseModel):
    lang: str
    title: str
    summary: str
    class Config:
        from_attributes = True


class StoryOut(BaseModel):
    id: int
    title: str
    summary: str
    category: str
    location_name: Optional[str] = None
    pincode: Optional[str] = None
    score: float
    official_confirmed: bool
    sources: List[SourceOut] = []
    translations: List[TranslationOut] = []


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class SourceIn(BaseModel):
    source_name: str
    is_official: bool = False
    agrees: bool = True


class StoryIn(BaseModel):
    title: str
    summary: str
    category: str
    location_name: Optional[str] = None
    pincode: Optional[str] = None
    score: float
    official_confirmed: bool = False
    published: bool = False
    sources: List[SourceIn] = []


def serialize(story: Story, lang: str = "en") -> StoryOut:
    title, summary = story.title, story.summary
    if lang != "en":
        match = next((t for t in story.translations if t.lang == lang), None)
        if match:
            title, summary = match.title, match.summary
    return StoryOut(id=story.id, title=title, summary=summary, category=story.category,
                     location_name=story.location_name, pincode=story.pincode, score=story.score,
                     official_confirmed=story.official_confirmed, sources=story.sources,
                     translations=story.translations)


# ============================== APP ==============================

app = FastAPI(title="NEONWIRE API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.on_event("startup")
def on_startup():
    create_admin_from_env()
    seed_demo_data()


@app.get("/stories", response_model=List[StoryOut])
def list_stories(category: Optional[str] = None, q: Optional[str] = None,
                  location: Optional[str] = None, lang: str = "en", db: Session = Depends(get_db)):
    query = db.query(Story).filter(Story.published == True)  # noqa: E712
    if category and category.lower() != "all":
        query = query.filter(Story.category.ilike(category))
    if q:
        like = f"%{q}%"
        query = query.filter(or_(Story.title.ilike(like), Story.summary.ilike(like)))
    if location:
        like = f"%{location}%"
        query = query.filter(or_(Story.location_name.ilike(like), Story.pincode.ilike(like)))
    return [serialize(s, lang) for s in query.order_by(Story.created_at.desc()).all()]


@app.get("/stories/{story_id}", response_model=StoryOut)
def get_story(story_id: int, lang: str = "en", db: Session = Depends(get_db)):
    story = db.query(Story).filter(Story.id == story_id, Story.published == True).first()  # noqa: E712
    if not story:
        raise HTTPException(status_code=404, detail="Story not found")
    return serialize(story, lang)


@app.post("/auth/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(AdminUser).filter(AdminUser.username == payload.username).first()
    if not user or not pwd_context.verify(payload.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Incorrect username or password")
    return TokenResponse(access_token=create_access_token(user.username))


@app.post("/admin/stories", response_model=StoryOut)
def create_story(payload: StoryIn, db: Session = Depends(get_db), _admin: AdminUser = Depends(require_admin)):
    story = Story(title=payload.title, summary=payload.summary, category=payload.category,
                  location_name=payload.location_name, pincode=payload.pincode, score=payload.score,
                  official_confirmed=payload.official_confirmed, published=payload.published)
    for s in payload.sources:
        story.sources.append(SourceRecord(**s.dict()))
    db.add(story)
    db.commit()
    db.refresh(story)
    return story


@app.put("/admin/stories/{story_id}/publish", response_model=StoryOut)
def publish_story(story_id: int, db: Session = Depends(get_db), _admin: AdminUser = Depends(require_admin)):
    story = db.query(Story).get(story_id)
    if not story:
        raise HTTPException(status_code=404, detail="Story not found")
    story.published = True
    db.commit()
    db.refresh(story)
    return story


@app.delete("/admin/stories/{story_id}")
def delete_story(story_id: int, db: Session = Depends(get_db), _admin: AdminUser = Depends(require_admin)):
    story = db.query(Story).get(story_id)
    if not story:
        raise HTTPException(status_code=404, detail="Story not found")
    db.delete(story)
    db.commit()
    return {"deleted": story_id}


@app.get("/health")
def health():
    return {"status": "ok"}


app.mount("/", StaticFiles(directory="static", html=True), name="frontend")
