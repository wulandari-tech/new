import json
import logging
import os
import sqlite3
import sys
from datetime import datetime

import mysql.connector
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import PyMongoError

_RUNTIME_CONFIG_DIR = os.path.join(os.path.dirname(__file__), "IvaSms-api")
if _RUNTIME_CONFIG_DIR not in sys.path:
    sys.path.insert(0, _RUNTIME_CONFIG_DIR)

from runtime_config import (
    DB_BACKEND,
    DB_PATH,
    IVASMS_STATE_DIR,
    LEGACY_SQLITE_DB_PATH,
    MONGODB_DB_NAME,
    MONGODB_URI,
    MYSQL_DATABASE,
    MYSQL_HOST,
    MYSQL_PASSWORD,
    MYSQL_PORT,
    MYSQL_USER,
    PRIVATE_DNS_PRIMARY,
    PRIVATE_DNS_SECONDARY,
)
from number_service import normalize_country_name

logger = logging.getLogger(__name__)


def _normalize_country_value(value):
    return normalize_country_name(value)


class Database:
    def __init__(self, db_path=DB_PATH):
        self.backend = DB_BACKEND
        self._state_dir = IVASMS_STATE_DIR
        self._legacy_db_path = LEGACY_SQLITE_DB_PATH or db_path
        self._profiles_file = os.path.join(self._state_dir, "user_profiles.json")
        self._withdrawals_file = os.path.join(self._state_dir, "withdrawals.json")
        os.makedirs(self._state_dir, exist_ok=True)
        self.conn = None
        self.mongo_client = None
        self.mongo_db = None
        self.numbers = None
        self.users = None
        self.user_profiles = None
        self.withdrawals = None
        self.counters = None
        self._connect_backend()
        self._migrate_legacy_data_if_needed()
        logger.info("Database initialized (%s)", self.backend)

    def _now(self):
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _connect_mysql(self):
        bootstrap = mysql.connector.connect(
            host=MYSQL_HOST,
            port=MYSQL_PORT,
            user=MYSQL_USER,
            password=MYSQL_PASSWORD,
            autocommit=True,
        )
        cur = bootstrap.cursor()
        cur.execute(
            f"CREATE DATABASE IF NOT EXISTS {MYSQL_DATABASE} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        )
        cur.close()
        bootstrap.close()
        return mysql.connector.connect(
            host=MYSQL_HOST,
            port=MYSQL_PORT,
            user=MYSQL_USER,
            password=MYSQL_PASSWORD,
            database=MYSQL_DATABASE,
            autocommit=False,
        )

    def _connect_backend(self):
        if self.backend == "mongodb":
            self._connect_mongodb()
            self._init_mongodb()
            return

        try:
            self.conn = self._connect_mysql()
            self._init_tables()
        except Exception as exc:
            if not MONGODB_URI:
                raise
            logger.warning(
                "MySQL init failed for backend=%s, falling back to MongoDB Atlas: %s",
                self.backend,
                exc,
            )
            self.backend = "mongodb"
            self.conn = None
            self._connect_mongodb()
            self._init_mongodb()

    def _connect_mongodb(self):
        if not MONGODB_URI:
            raise RuntimeError("IVASMS_MONGODB_URI is required when IVASMS_DB_BACKEND=mongodb")
        import dns.resolver

        original_resolver = dns.resolver.default_resolver
        last_error = None
        for use_private_dns in (True, False):
            try:
                if use_private_dns:
                    resolver = dns.resolver.Resolver(configure=False)
                    resolver.nameservers = [PRIVATE_DNS_PRIMARY, PRIVATE_DNS_SECONDARY]
                    dns.resolver.default_resolver = resolver
                    logger.info(
                        "MongoDB DNS resolver pinned to %s, %s",
                        PRIVATE_DNS_PRIMARY,
                        PRIVATE_DNS_SECONDARY,
                    )
                else:
                    dns.resolver.default_resolver = dns.resolver.Resolver()
                    logger.warning("MongoDB private DNS failed, falling back to system resolver")

                self.mongo_client = MongoClient(MONGODB_URI, serverSelectionTimeoutMS=30000, retryWrites=True)
                self.mongo_db = self.mongo_client[MONGODB_DB_NAME]
                self.users = self.mongo_db["users"]
                self.numbers = self.mongo_db["numbers"]
                self.user_profiles = self.mongo_db["user_profiles"]
                self.withdrawals = self.mongo_db["withdrawals"]
                self.counters = self.mongo_db["counters"]
                self.mongo_client.admin.command("ping")
                return
            except Exception as exc:
                last_error = exc
                if self.mongo_client:
                    try:
                        self.mongo_client.close()
                    except Exception:
                        pass
                self.mongo_client = None
                self.mongo_db = None
                self.users = None
                self.numbers = None
                self.user_profiles = None
                self.withdrawals = None
                self.counters = None
        dns.resolver.default_resolver = original_resolver
        raise last_error

    def _init_mongodb(self):
        self._repair_mongodb_schema()
        self.users.create_index(
            "telegram_id",
            unique=True,
            partialFilterExpression={"telegram_id": {"$type": ["int", "long"]}},
        )
        self.numbers.create_index("phone_number", unique=True, sparse=True)
        self.numbers.create_index([("status", ASCENDING), ("country", ASCENDING)])
        self.numbers.create_index([("assigned_to", ASCENDING), ("status", ASCENDING)])
        self.user_profiles.create_index(
            "telegram_id",
            unique=True,
            partialFilterExpression={"telegram_id": {"$type": ["int", "long"]}},
        )
        self.withdrawals.create_index("request_id", unique=True, sparse=True)
        self.withdrawals.create_index([("telegram_id", ASCENDING), ("status", ASCENDING)])

    def _repair_mongodb_schema(self):
        self._repair_mongo_collection(self.users, "users")
        self._repair_mongo_collection(self.user_profiles, "user_profiles")
        self._repair_mongo_collection(self.withdrawals, "withdrawals")

    def _repair_mongo_collection(self, collection, collection_name):
        legacy_indexes = []
        for index in collection.list_indexes():
            name = index.get("name", "")
            key_names = list((index.get("key") or {}).keys())
            if "telegramId" in key_names or name.startswith("telegramId_"):
                legacy_indexes.append(name)

        for index_name in legacy_indexes:
            try:
                collection.drop_index(index_name)
                logger.info("Dropped legacy MongoDB index %s on %s", index_name, collection_name)
            except Exception as exc:
                logger.warning(
                    "Failed to drop legacy MongoDB index %s on %s: %s",
                    index_name,
                    collection_name,
                    exc,
                )

        for document in collection.find(
            {"$or": [{"telegramId": {"$exists": True}}, {"telegram_id": {"$exists": False}}]}
        ):
            document_id = document["_id"]
            telegram_value = document.get("telegram_id", document.get("telegramId"))
            update_payload = {}
            unset_payload = {}
            if telegram_value is not None:
                try:
                    update_payload["telegram_id"] = int(telegram_value)
                except (TypeError, ValueError):
                    logger.warning(
                        "Skipping invalid telegram id in %s document %s: %r",
                        collection_name,
                        document_id,
                        telegram_value,
                    )
            if "telegramId" in document:
                unset_payload["telegramId"] = ""

            target_telegram_id = update_payload.get("telegram_id")
            if target_telegram_id is not None:
                existing = collection.find_one(
                    {
                        "_id": {"$ne": document_id},
                        "telegram_id": target_telegram_id,
                    }
                )
                if existing:
                    merged_payload = {}
                    for key, value in document.items():
                        if key in {"_id", "telegramId"}:
                            continue
                        if existing.get(key) in (None, "", 0) and value not in (None, ""):
                            merged_payload[key] = value
                    if merged_payload:
                        collection.update_one({"_id": existing["_id"]}, {"$set": merged_payload})
                    collection.delete_one({"_id": document_id})
                    logger.warning(
                        "Merged duplicate %s document %s into %s for telegram_id=%s",
                        collection_name,
                        document_id,
                        existing["_id"],
                        target_telegram_id,
                    )
                    continue

            if update_payload or unset_payload:
                update_doc = {}
                if update_payload:
                    update_doc["$set"] = update_payload
                if unset_payload:
                    update_doc["$unset"] = unset_payload
                collection.update_one({"_id": document_id}, update_doc)

    def _q(self, query, params=(), one=False, many=False, commit=False):
        cur = self.conn.cursor(dictionary=True)
        cur.execute(query, params)
        data = None
        if one:
            data = cur.fetchone()
        elif many:
            data = cur.fetchall()
        rowcount = cur.rowcount
        lastrowid = cur.lastrowid
        if commit:
            self.conn.commit()
        cur.close()
        return data, rowcount, lastrowid

    def _scalar(self, query, params=()):
        row, _, _ = self._q(query, params, one=True)
        return None if row is None else next(iter(row.values()))

    def _mongo_next_sequence(self, name):
        doc = self.counters.find_one_and_update(
            {"_id": name},
            {"$inc": {"value": 1}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        return int(doc.get("value", 1))

    def _mongo_clean(self, document):
        if not document:
            return None
        doc = dict(document)
        doc.pop("_id", None)
        return doc

    def _mongo_clean_many(self, cursor):
        return [self._mongo_clean(item) for item in cursor]

    def _init_tables(self):
        statements = [
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_id BIGINT PRIMARY KEY,
                username VARCHAR(255),
                first_name VARCHAR(255),
                joined_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                total_numbers INT DEFAULT 0
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """,
            """
            CREATE TABLE IF NOT EXISTS numbers (
                id BIGINT PRIMARY KEY AUTO_INCREMENT,
                phone_number VARCHAR(64) UNIQUE,
                country VARCHAR(255) NOT NULL,
                range_name VARCHAR(255),
                rate DECIMAL(12,2) DEFAULT 0,
                status VARCHAR(32) DEFAULT 'available',
                assigned_to BIGINT,
                assigned_at DATETIME NULL,
                otp_message TEXT,
                otp_received_at DATETIME NULL,
                INDEX idx_numbers_status (status),
                INDEX idx_numbers_country (country),
                INDEX idx_numbers_assigned (assigned_to)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """,
            """
            CREATE TABLE IF NOT EXISTS user_profiles (
                telegram_id BIGINT PRIMARY KEY,
                ref_code VARCHAR(64),
                referred_by BIGINT NULL,
                referral_count INT DEFAULT 0,
                referral_bonus INT DEFAULT 0,
                balance_usd DECIMAL(12,2) DEFAULT 0,
                balance_dana BIGINT DEFAULT 0,
                withdraw_method VARCHAR(32) NULL,
                withdraw_name VARCHAR(255) DEFAULT '',
                withdraw_account VARCHAR(255) DEFAULT '',
                withdraw_pending_id VARCHAR(64) NULL
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """,
            """
            CREATE TABLE IF NOT EXISTS withdrawals (
                request_id VARCHAR(64) PRIMARY KEY,
                telegram_id BIGINT NOT NULL,
                method VARCHAR(32) NOT NULL,
                amount DECIMAL(12,2) DEFAULT 0,
                destination_label TEXT,
                status VARCHAR(32) DEFAULT 'pending',
                created_at DATETIME,
                updated_at DATETIME NULL,
                admin_note TEXT,
                INDEX idx_withdrawals_user (telegram_id),
                INDEX idx_withdrawals_status (status)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
            """,
        ]
        for sql in statements:
            self._q(sql, commit=True)

    def _candidate_legacy_sqlite_paths(self):
        paths = [self._legacy_db_path, os.path.join(os.getcwd(), "ivasms.db")]
        seen = set()
        result = []
        for path in paths:
            if not path:
                continue
            full = os.path.abspath(path)
            if full in seen:
                continue
            seen.add(full)
            if os.path.exists(full):
                result.append(full)
        return result

    def _profile_upsert(self, profile):
        if self.backend == "mongodb":
            payload = {
                "telegram_id": int(profile["telegram_id"]),
                "ref_code": profile.get("ref_code") or self._build_ref_code(profile["telegram_id"]),
                "referred_by": profile.get("referred_by"),
                "referral_count": int(profile.get("referral_count", 0) or 0),
                "referral_bonus": int(profile.get("referral_bonus", 0) or 0),
                "balance_usd": float(profile.get("balance_usd", 0) or 0),
                "balance_dana": int(profile.get("balance_dana", 0) or 0),
                "withdraw_method": profile.get("withdraw_method"),
                "withdraw_name": profile.get("withdraw_name", "") or "",
                "withdraw_account": profile.get("withdraw_account", "") or "",
                "withdraw_pending_id": profile.get("withdraw_pending_id"),
            }
            self.user_profiles.update_one({"telegram_id": payload["telegram_id"]}, {"$set": payload}, upsert=True)
            return
        self._q(
            """
            INSERT INTO user_profiles (
                telegram_id, ref_code, referred_by, referral_count,
                referral_bonus, balance_usd, balance_dana, withdraw_method,
                withdraw_name, withdraw_account, withdraw_pending_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                ref_code = VALUES(ref_code),
                referred_by = VALUES(referred_by),
                referral_count = VALUES(referral_count),
                referral_bonus = VALUES(referral_bonus),
                balance_usd = VALUES(balance_usd),
                balance_dana = VALUES(balance_dana),
                withdraw_method = VALUES(withdraw_method),
                withdraw_name = VALUES(withdraw_name),
                withdraw_account = VALUES(withdraw_account),
                withdraw_pending_id = VALUES(withdraw_pending_id)
            """,
            (
                int(profile["telegram_id"]),
                profile.get("ref_code") or self._build_ref_code(profile["telegram_id"]),
                profile.get("referred_by"),
                int(profile.get("referral_count", 0) or 0),
                int(profile.get("referral_bonus", 0) or 0),
                float(profile.get("balance_usd", 0) or 0),
                int(profile.get("balance_dana", 0) or 0),
                profile.get("withdraw_method"),
                profile.get("withdraw_name", "") or "",
                profile.get("withdraw_account", "") or "",
                profile.get("withdraw_pending_id"),
            ),
        )

    def _withdrawal_upsert(self, request):
        if self.backend == "mongodb":
            payload = {
                "request_id": request["request_id"],
                "telegram_id": int(request["telegram_id"]),
                "method": request.get("method"),
                "amount": float(request.get("amount", 0) or 0),
                "destination_label": request.get("destination_label"),
                "status": request.get("status", "pending"),
                "created_at": request.get("created_at") or self._now(),
                "updated_at": request.get("updated_at"),
                "admin_note": request.get("admin_note"),
            }
            self.withdrawals.update_one({"request_id": payload["request_id"]}, {"$set": payload}, upsert=True)
            return
        self._q(
            """
            INSERT INTO withdrawals (
                request_id, telegram_id, method, amount, destination_label,
                status, created_at, updated_at, admin_note
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                telegram_id = VALUES(telegram_id),
                method = VALUES(method),
                amount = VALUES(amount),
                destination_label = VALUES(destination_label),
                status = VALUES(status),
                created_at = VALUES(created_at),
                updated_at = VALUES(updated_at),
                admin_note = VALUES(admin_note)
            """,
            (
                request["request_id"],
                int(request["telegram_id"]),
                request.get("method"),
                float(request.get("amount", 0) or 0),
                request.get("destination_label"),
                request.get("status", "pending"),
                request.get("created_at") or self._now(),
                request.get("updated_at"),
                request.get("admin_note"),
            ),
        )

    def _build_ref_code(self, telegram_id):
        return f"REF{int(telegram_id)}"

    def _migrate_legacy_data_if_needed(self):
        if self.backend == "mongodb":
            users = int(self.users.count_documents({}))
            numbers = int(self.numbers.count_documents({}))
            profiles = int(self.user_profiles.count_documents({}))
            withdrawals = int(self.withdrawals.count_documents({}))
        else:
            users = int(self._scalar("SELECT COUNT(*) FROM users") or 0)
            numbers = int(self._scalar("SELECT COUNT(*) FROM numbers") or 0)
            profiles = int(self._scalar("SELECT COUNT(*) FROM user_profiles") or 0)
            withdrawals = int(self._scalar("SELECT COUNT(*) FROM withdrawals") or 0)
        if users == 0 and numbers == 0:
            self._import_legacy_sqlite()
        if profiles == 0:
            self._import_profiles_json()
        if withdrawals == 0:
            self._import_withdrawals_json()

    def _import_legacy_sqlite(self):
        source = None
        for candidate in self._candidate_legacy_sqlite_paths():
            source = candidate
            break
        if not source:
            return

        logger.info(f"Importing legacy SQLite data from {source}")
        legacy = sqlite3.connect(source)
        legacy.row_factory = sqlite3.Row
        cur = legacy.cursor()
        if self.backend == "mongodb":
            try:
                cur.execute("SELECT telegram_id, username, first_name, joined_at, total_numbers FROM users")
                for row in cur.fetchall():
                    self.users.update_one(
                        {"telegram_id": int(row["telegram_id"])},
                        {
                            "$set": {
                                "telegram_id": int(row["telegram_id"]),
                                "username": row["username"],
                                "first_name": row["first_name"],
                                "joined_at": row["joined_at"] or self._now(),
                                "total_numbers": int(row["total_numbers"] or 0),
                            }
                        },
                        upsert=True,
                    )
            except sqlite3.OperationalError:
                pass

            try:
                cur.execute(
                    """
                    SELECT phone_number, country, range_name, rate, status,
                           assigned_to, assigned_at, otp_message, otp_received_at
                    FROM numbers
                    """
                )
                for row in cur.fetchall():
                    existing = self.numbers.find_one({"phone_number": row["phone_number"]})
                    number_id = int(existing["id"]) if existing and existing.get("id") else self._mongo_next_sequence("numbers")
                    self.numbers.update_one(
                        {"phone_number": row["phone_number"]},
                        {
                            "$set": {
                                "id": number_id,
                                "phone_number": row["phone_number"],
                                "country": _normalize_country_value(row["country"]),
                                "range_name": row["range_name"],
                                "rate": float(row["rate"] or 0),
                                "status": row["status"] or "available",
                                "assigned_to": row["assigned_to"],
                                "assigned_at": row["assigned_at"],
                                "otp_message": row["otp_message"],
                                "otp_received_at": row["otp_received_at"],
                            }
                        },
                        upsert=True,
                    )
            except sqlite3.OperationalError:
                pass
            legacy.close()
            return

        try:
            cur.execute("SELECT telegram_id, username, first_name, joined_at, total_numbers FROM users")
            for row in cur.fetchall():
                self._q(
                    """
                    INSERT INTO users (telegram_id, username, first_name, joined_at, total_numbers)
                    VALUES (%s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        username = VALUES(username),
                        first_name = VALUES(first_name),
                        total_numbers = VALUES(total_numbers)
                    """,
                    (
                        int(row["telegram_id"]),
                        row["username"],
                        row["first_name"],
                        row["joined_at"] or self._now(),
                        int(row["total_numbers"] or 0),
                    ),
                )
        except sqlite3.OperationalError:
            pass

        try:
            cur.execute(
                """
                SELECT phone_number, country, range_name, rate, status,
                       assigned_to, assigned_at, otp_message, otp_received_at
                FROM numbers
                """
            )
            for row in cur.fetchall():
                self._q(
                    """
                    INSERT INTO numbers (
                        phone_number, country, range_name, rate, status,
                        assigned_to, assigned_at, otp_message, otp_received_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        country = VALUES(country),
                        range_name = VALUES(range_name),
                        rate = VALUES(rate),
                        status = VALUES(status),
                        assigned_to = VALUES(assigned_to),
                        assigned_at = VALUES(assigned_at),
                        otp_message = VALUES(otp_message),
                        otp_received_at = VALUES(otp_received_at)
                    """,
                    (
                        row["phone_number"],
                        row["country"],
                        row["range_name"],
                        float(row["rate"] or 0),
                        row["status"] or "available",
                        row["assigned_to"],
                        row["assigned_at"],
                        row["otp_message"],
                        row["otp_received_at"],
                    ),
                )
        except sqlite3.OperationalError:
            pass
        legacy.close()
        if self.conn:
            self.conn.commit()

    def _import_profiles_json(self):
        if not os.path.exists(self._profiles_file):
            return
        try:
            with open(self._profiles_file, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception:
            return
        if not isinstance(payload, dict):
            return
        for key, meta in payload.items():
            try:
                telegram_id = int(key)
            except Exception:
                continue
            self._profile_upsert(
                {
                    "telegram_id": telegram_id,
                    "ref_code": meta.get("ref_code") or self._build_ref_code(telegram_id),
                    "referred_by": meta.get("referred_by"),
                    "referral_count": int(meta.get("referral_count", 0) or 0),
                    "referral_bonus": int(meta.get("referral_bonus", 0) or 0),
                    "balance_usd": float(meta.get("balance_usd", 0) or 0),
                    "balance_dana": int(meta.get("balance_dana", 0) or 0),
                    "withdraw_method": meta.get("withdraw_method"),
                    "withdraw_name": meta.get("withdraw_name", "") or "",
                    "withdraw_account": meta.get("withdraw_account", "") or "",
                    "withdraw_pending_id": meta.get("withdraw_pending_id"),
                }
            )
        if self.conn:
            self.conn.commit()

    def _import_withdrawals_json(self):
        if not os.path.exists(self._withdrawals_file):
            return
        try:
            with open(self._withdrawals_file, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception:
            return
        if not isinstance(payload, dict):
            return
        for request_id, request in payload.items():
            self._withdrawal_upsert(
                {
                    "request_id": request_id,
                    "telegram_id": int(request.get("telegram_id", 0) or 0),
                    "method": request.get("method", ""),
                    "amount": float(request.get("amount", 0) or 0),
                    "destination_label": request.get("destination_label"),
                    "status": request.get("status", "pending"),
                    "created_at": request.get("created_at") or self._now(),
                    "updated_at": request.get("updated_at"),
                    "admin_note": request.get("admin_note"),
                }
            )
        if self.conn:
            self.conn.commit()

    def get_or_create_user(self, telegram_id, username="", first_name=""):
        telegram_id = int(telegram_id)
        if self.backend == "mongodb":
            existing = self.users.find_one({"telegram_id": telegram_id})
            payload = {
                "telegram_id": telegram_id,
                "username": username,
                "first_name": first_name,
                "joined_at": (existing or {}).get("joined_at") or self._now(),
                "total_numbers": int((existing or {}).get("total_numbers", 0) or 0),
            }
            self.users.update_one({"telegram_id": telegram_id}, {"$set": payload}, upsert=True)
            meta = self._ensure_profile_meta(telegram_id)
            user = self._mongo_clean(self.users.find_one({"telegram_id": telegram_id}))
            user.update(meta)
            return user
        user, _, _ = self._q("SELECT * FROM users WHERE telegram_id = %s", (telegram_id,), one=True)
        if not user:
            self._q(
                """
                INSERT INTO users (telegram_id, username, first_name, joined_at, total_numbers)
                VALUES (%s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE username = VALUES(username), first_name = VALUES(first_name)
                """,
                (telegram_id, username, first_name, self._now(), 0),
            )
            self.conn.commit()
        else:
            self._q(
                "UPDATE users SET username = %s, first_name = %s WHERE telegram_id = %s",
                (username, first_name, telegram_id),
                commit=True,
            )
        meta = self._ensure_profile_meta(telegram_id)
        user, _, _ = self._q("SELECT * FROM users WHERE telegram_id = %s", (telegram_id,), one=True)
        user.update(meta)
        return user

    def increment_user_numbers(self, telegram_id):
        telegram_id = int(telegram_id)
        if self.backend == "mongodb":
            self.users.update_one({"telegram_id": telegram_id}, {"$inc": {"total_numbers": 1}}, upsert=True)
            return
        self._q(
            "UPDATE users SET total_numbers = total_numbers + 1 WHERE telegram_id = %s",
            (telegram_id,),
            commit=True,
        )

    def get_all_user_ids(self):
        if self.backend == "mongodb":
            rows = self.users.find(
                {"telegram_id": {"$type": ["int", "long"]}},
                {"telegram_id": 1},
            ).sort("joined_at", ASCENDING)
            return [int(row["telegram_id"]) for row in rows if row.get("telegram_id") is not None]
        rows, _, _ = self._q(
            "SELECT telegram_id FROM users ORDER BY joined_at ASC",
            many=True,
        )
        return [int(row["telegram_id"]) for row in rows if row.get("telegram_id") is not None]

    def _ensure_profile_meta(self, telegram_id):
        if self.backend == "mongodb":
            profile = self._mongo_clean(self.user_profiles.find_one({"telegram_id": telegram_id}))
            if profile:
                if not profile.get("ref_code"):
                    profile["ref_code"] = self._build_ref_code(telegram_id)
                    self._profile_upsert(profile)
                return profile
            profile = {
                "telegram_id": int(telegram_id),
                "ref_code": self._build_ref_code(telegram_id),
                "referred_by": None,
                "referral_count": 0,
                "referral_bonus": 0,
                "balance_usd": 0.0,
                "balance_dana": 0,
                "withdraw_method": None,
                "withdraw_name": "",
                "withdraw_account": "",
                "withdraw_pending_id": None,
            }
            self._profile_upsert(profile)
            return profile
        profile, _, _ = self._q(
            "SELECT * FROM user_profiles WHERE telegram_id = %s",
            (telegram_id,),
            one=True,
        )
        if profile:
            if not profile.get("ref_code"):
                profile["ref_code"] = self._build_ref_code(telegram_id)
                self._profile_upsert(profile)
                self.conn.commit()
            return profile
        profile = {
            "telegram_id": int(telegram_id),
            "ref_code": self._build_ref_code(telegram_id),
            "referred_by": None,
            "referral_count": 0,
            "referral_bonus": 0,
            "balance_usd": 0.0,
            "balance_dana": 0,
            "withdraw_method": None,
            "withdraw_name": "",
            "withdraw_account": "",
            "withdraw_pending_id": None,
        }
        self._profile_upsert(profile)
        self.conn.commit()
        return profile

    def update_profile_meta(self, telegram_id, **updates):
        profile = self._ensure_profile_meta(telegram_id)
        profile.update(updates)
        self._profile_upsert(profile)
        if self.conn:
            self.conn.commit()
        return profile

    def add_numbers(self, numbers_list):
        return self.add_numbers_report(numbers_list)["added"]

    def add_numbers_report(self, numbers_list):
        added = 0
        reactivated = 0
        duplicates = 0
        duplicate_samples = []
        country_changes = {}

        def touch_country(country):
            entry = country_changes.get(country)
            if not entry:
                entry = {"added": 0, "reactivated": 0}
                country_changes[country] = entry
            return entry

        for num in numbers_list:
            num["country"] = _normalize_country_value(num.get("country"))
            if self.backend == "mongodb":
                existing = self._mongo_clean(self.numbers.find_one({"phone_number": num["phone_number"]}))
                if not existing:
                    self.numbers.insert_one(
                        {
                            "id": self._mongo_next_sequence("numbers"),
                            "phone_number": num["phone_number"],
                            "country": num["country"],
                            "range_name": num["range_name"],
                            "rate": float(num.get("rate", 0) or 0),
                            "status": "available",
                            "assigned_to": None,
                            "assigned_at": None,
                            "otp_message": None,
                            "otp_received_at": None,
                        }
                    )
                    added += 1
                    touch_country(num["country"])["added"] += 1
                    continue
                should_reactivate = (
                    existing["status"] != "available"
                    or existing["country"] != num["country"]
                    or (existing.get("range_name") or "") != (num["range_name"] or "")
                    or float(existing.get("rate") or 0) != float(num.get("rate", 0) or 0)
                )
                if should_reactivate:
                    self.numbers.update_one(
                        {"id": existing["id"]},
                        {
                            "$set": {
                                "country": num["country"],
                                "range_name": num["range_name"],
                                "rate": float(num.get("rate", 0) or 0),
                                "status": "available",
                                "assigned_to": None,
                                "assigned_at": None,
                                "otp_message": None,
                                "otp_received_at": None,
                            }
                        },
                    )
                    reactivated += 1
                    touch_country(num["country"])["reactivated"] += 1
                else:
                    duplicates += 1
                    if len(duplicate_samples) < 5:
                        duplicate_samples.append(num["phone_number"])
                continue
            existing, _, _ = self._q(
                "SELECT id, status, country, range_name, rate FROM numbers WHERE phone_number = %s",
                (num["phone_number"],),
                one=True,
            )
            if not existing:
                self._q(
                    "INSERT INTO numbers (phone_number, country, range_name, rate, status) VALUES (%s, %s, %s, %s, 'available')",
                    (
                        num["phone_number"],
                        num["country"],
                        num["range_name"],
                        float(num.get("rate", 0) or 0),
                    ),
                )
                added += 1
                touch_country(num["country"])["added"] += 1
                continue
            should_reactivate = (
                existing["status"] != "available"
                or existing["country"] != num["country"]
                or (existing.get("range_name") or "") != (num["range_name"] or "")
                or float(existing.get("rate") or 0) != float(num.get("rate", 0) or 0)
            )
            if should_reactivate:
                self._q(
                    """
                    UPDATE numbers
                    SET country = %s, range_name = %s, rate = %s, status = 'available',
                        assigned_to = NULL, assigned_at = NULL,
                        otp_message = NULL, otp_received_at = NULL
                    WHERE id = %s
                    """,
                    (
                        num["country"],
                        num["range_name"],
                        float(num.get("rate", 0) or 0),
                        existing["id"],
                    ),
                )
                reactivated += 1
                touch_country(num["country"])["reactivated"] += 1
            else:
                duplicates += 1
                if len(duplicate_samples) < 5:
                    duplicate_samples.append(num["phone_number"])
        if self.conn:
            self.conn.commit()
        return {
            "total": len(numbers_list),
            "added": added,
            "reactivated": reactivated,
            "duplicates": duplicates,
            "duplicate_samples": duplicate_samples,
            "country_changes": country_changes,
        }

    def get_stock_by_country(self):
        if self.backend == "mongodb":
            rows = self.numbers.aggregate(
                [
                    {"$match": {"status": "available"}},
                    {"$group": {"_id": "$country", "count": {"$sum": 1}}},
                    {"$sort": {"_id": 1}},
                ]
            )
            return {row["_id"]: row["count"] for row in rows}
        rows, _, _ = self._q(
            "SELECT country, COUNT(*) AS count FROM numbers WHERE status = 'available' GROUP BY country ORDER BY country",
            many=True,
        )
        return {row["country"]: row["count"] for row in rows}

    def get_total_stock(self):
        if self.backend == "mongodb":
            return int(self.numbers.count_documents({"status": "available"}))
        return int(self._scalar("SELECT COUNT(*) FROM numbers WHERE status = 'available'") or 0)

    def get_assigned_count(self):
        if self.backend == "mongodb":
            return int(self.numbers.count_documents({"status": "assigned"}))
        return int(self._scalar("SELECT COUNT(*) FROM numbers WHERE status = 'assigned'") or 0)

    def delete_available_numbers(self, country=None):
        if self.backend == "mongodb":
            query = {"status": "available"}
            if country:
                query["country"] = _normalize_country_value(country)
            result = self.numbers.delete_many(query)
            return int(result.deleted_count or 0)
        if country:
            _, rowcount, _ = self._q(
                "DELETE FROM numbers WHERE status = 'available' AND country = %s",
                (country,),
                commit=True,
            )
            return int(rowcount or 0)

        _, rowcount, _ = self._q(
            "DELETE FROM numbers WHERE status = 'available'",
            commit=True,
        )
        return int(rowcount or 0)

    def assign_number(self, telegram_id, country):
        country = _normalize_country_value(country)
        if self.backend == "mongodb":
            number = self._mongo_clean(self.numbers.find_one({"status": "available", "country": country}))
            if not number:
                return None
            self.numbers.update_one(
                {"id": number["id"]},
                {"$set": {"status": "assigned", "assigned_to": telegram_id, "assigned_at": self._now()}},
            )
            number["status"] = "assigned"
            number["assigned_to"] = telegram_id
            number["assigned_at"] = self._now()
            return number
        number, _, _ = self._q(
            "SELECT * FROM numbers WHERE status = 'available' AND country = %s ORDER BY RAND() LIMIT 1",
            (country,),
            one=True,
        )
        if not number:
            return None
        self._q(
            "UPDATE numbers SET status = 'assigned', assigned_to = %s, assigned_at = %s WHERE id = %s",
            (telegram_id, self._now(), number["id"]),
        )
        self.conn.commit()
        return number

    def get_user_active_number(self, telegram_id):
        if self.backend == "mongodb":
            row = self.users.find_one({"telegram_id": telegram_id})
            _ = row
            return self._mongo_clean(
                self.numbers.find_one(
                    {"assigned_to": telegram_id, "status": "assigned"},
                    sort=[("assigned_at", DESCENDING)],
                )
            )
        row, _, _ = self._q(
            "SELECT * FROM numbers WHERE assigned_to = %s AND status = 'assigned' ORDER BY assigned_at DESC LIMIT 1",
            (telegram_id,),
            one=True,
        )
        return row

    def get_user_active_count(self, telegram_id):
        if self.backend == "mongodb":
            return int(self.numbers.count_documents({"assigned_to": telegram_id, "status": "assigned"}))
        return int(self._scalar("SELECT COUNT(*) FROM numbers WHERE assigned_to = %s AND status = 'assigned'", (telegram_id,)) or 0)

    def release_number(self, number_id):
        if self.backend == "mongodb":
            self.numbers.update_one(
                {"id": number_id},
                {"$set": {"status": "available", "assigned_to": None, "assigned_at": None}},
            )
            return
        self._q(
            "UPDATE numbers SET status = 'available', assigned_to = NULL, assigned_at = NULL WHERE id = %s",
            (number_id,),
            commit=True,
        )

    def mark_number_used(self, number_id, otp_message=""):
        if self.backend == "mongodb":
            self.numbers.update_one(
                {"id": number_id},
                {"$set": {"status": "used", "otp_message": otp_message, "otp_received_at": self._now()}},
            )
            return
        self._q(
            "UPDATE numbers SET status = 'used', otp_message = %s, otp_received_at = %s WHERE id = %s",
            (otp_message, self._now(), number_id),
            commit=True,
        )

    def save_otp(self, phone_number, otp_message):
        if self.backend == "mongodb":
            doc = self.numbers.find_one_and_update(
                {"phone_number": phone_number, "status": "assigned"},
                {"$set": {"otp_message": otp_message, "otp_received_at": self._now(), "status": "used"}},
                return_document=ReturnDocument.AFTER,
            )
            return self._mongo_clean(doc)
        _, rowcount, _ = self._q(
            "UPDATE numbers SET otp_message = %s, otp_received_at = %s, status = 'used' WHERE phone_number = %s AND status = 'assigned'",
            (otp_message, self._now(), phone_number),
            commit=True,
        )
        if rowcount > 0:
            row, _, _ = self._q("SELECT * FROM numbers WHERE phone_number = %s", (phone_number,), one=True)
            return row
        return None

    def check_number_otp(self, number_id):
        if self.backend == "mongodb":
            return self._mongo_clean(self.numbers.find_one({"id": number_id}))
        row, _, _ = self._q("SELECT * FROM numbers WHERE id = %s", (number_id,), one=True)
        return row

    def get_pending_assigned_numbers(self):
        if self.backend == "mongodb":
            rows = self.numbers.find({"status": "assigned"}).sort("assigned_at", ASCENDING)
            return self._mongo_clean_many(rows)
        rows, _, _ = self._q(
            "SELECT * FROM numbers WHERE status = 'assigned' ORDER BY assigned_at ASC",
            many=True,
        )
        return rows

    def change_number(self, telegram_id, country):
        current = self.get_user_active_number(telegram_id)
        if current:
            self.release_number(current["id"])
        return self.assign_number(telegram_id, country)

    def get_leaderboard(self, limit=10):
        if self.backend == "mongodb":
            rows = self.users.find({"total_numbers": {"$gt": 0}}).sort("total_numbers", DESCENDING).limit(limit)
            return self._mongo_clean_many(rows)
        rows, _, _ = self._q(
            "SELECT telegram_id, username, first_name, total_numbers FROM users WHERE total_numbers > 0 ORDER BY total_numbers DESC LIMIT %s",
            (limit,),
            many=True,
        )
        return rows

    def get_user_profile(self, telegram_id):
        if self.backend == "mongodb":
            user = self._mongo_clean(self.users.find_one({"telegram_id": telegram_id}))
            if not user:
                return None
            user.update(self._ensure_profile_meta(telegram_id))
            user["active_numbers"] = self.get_user_active_count(telegram_id)
            return user
        user, _, _ = self._q("SELECT * FROM users WHERE telegram_id = %s", (telegram_id,), one=True)
        if not user:
            return None
        user.update(self._ensure_profile_meta(telegram_id))
        user["active_numbers"] = self.get_user_active_count(telegram_id)
        return user

    def get_referral_leaderboard(self, limit=10):
        if self.backend == "mongodb":
            rows = []
            for profile in self.user_profiles.find(
                {"$or": [{"referral_count": {"$gt": 0}}, {"referral_bonus": {"$gt": 0}}]}
            ):
                user = self._mongo_clean(self.users.find_one({"telegram_id": profile["telegram_id"]})) or {}
                merged = {**user, **self._mongo_clean(profile)}
                rows.append(merged)
            rows.sort(
                key=lambda item: (
                    int(item.get("referral_count", 0) or 0),
                    int(item.get("referral_bonus", 0) or 0),
                    int(item.get("total_numbers", 0) or 0),
                ),
                reverse=True,
            )
            return rows[:limit]
        rows, _, _ = self._q(
            """
            SELECT
                u.telegram_id,
                u.username,
                u.first_name,
                u.total_numbers,
                p.ref_code,
                p.referred_by,
                p.referral_count,
                p.referral_bonus,
                p.balance_usd,
                p.balance_dana,
                p.withdraw_method,
                p.withdraw_name,
                p.withdraw_account,
                p.withdraw_pending_id
            FROM user_profiles p
            JOIN users u ON u.telegram_id = p.telegram_id
            WHERE p.referral_count > 0 OR p.referral_bonus > 0
            ORDER BY p.referral_count DESC, p.referral_bonus DESC, u.total_numbers DESC
            LIMIT %s
            """,
            (limit,),
            many=True,
        )
        return rows

    def create_withdraw_request(self, telegram_id, method, amount, destination_label):
        self._ensure_profile_meta(telegram_id)
        request_id = f"WD{telegram_id}{int(datetime.now().timestamp())}"
        request = {
            "request_id": request_id,
            "telegram_id": int(telegram_id),
            "method": method,
            "amount": float(amount),
            "destination_label": destination_label,
            "status": "pending",
            "created_at": self._now(),
            "updated_at": None,
            "admin_note": None,
        }
        self._withdrawal_upsert(request)
        self.update_profile_meta(telegram_id, withdraw_pending_id=request_id)
        if self.conn:
            self.conn.commit()
        return request

    def get_withdraw_request(self, request_id):
        if self.backend == "mongodb":
            return self._mongo_clean(self.withdrawals.find_one({"request_id": request_id}))
        row, _, _ = self._q(
            "SELECT * FROM withdrawals WHERE request_id = %s",
            (request_id,),
            one=True,
        )
        return row

    def update_withdraw_request(self, request_id, **updates):
        request = self.get_withdraw_request(request_id)
        if not request:
            return None
        request.update(updates)
        request["updated_at"] = self._now()
        self._withdrawal_upsert(request)
        if self.conn:
            self.conn.commit()
        return request

    def clear_withdraw_pending(self, telegram_id, request_id=None):
        profile = self._ensure_profile_meta(telegram_id)
        if request_id and profile.get("withdraw_pending_id") != request_id:
            return profile
        return self.update_profile_meta(telegram_id, withdraw_pending_id=None)

    def close(self):
        if self.mongo_client:
            self.mongo_client.close()
        if self.conn:
            self.conn.close()
