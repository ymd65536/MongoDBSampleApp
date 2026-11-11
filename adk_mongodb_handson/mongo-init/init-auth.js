// auth profile 用の初期化スクリプト (初回起動時のみ root 権限で実行される)
// パスワードはすべて環境変数 (.env) から取得する。
const dbName = process.env.HANDSON_AUTH_DB || "adk_handson_auth";
const target = db.getSiblingDB(dbName);

// 1. アプリ用: 対象 DB に限定した readWrite
target.createUser({
  user: "adk_app",
  pwd: process.env.ADK_APP_PASSWORD,
  roles: [{ role: "readWrite", db: dbName }],
});

// 2. 参照専用: 検索ツール (MongoDbToolset) 向けの read ロール
target.createUser({
  user: "adk_reader",
  pwd: process.env.ADK_READER_PASSWORD,
  roles: [{ role: "read", db: dbName }],
});

// 3. createIndex 権限なし: ensure_indexes=True 時の警告フォールバック確認用
target.createRole({
  role: "adkNoIndex",
  privileges: [
    {
      resource: { db: dbName, collection: "" },
      actions: ["find", "insert", "update", "remove"],
    },
  ],
  roles: [],
});
target.createUser({
  user: "adk_noindex",
  pwd: process.env.ADK_NOINDEX_PASSWORD,
  roles: [{ role: "adkNoIndex", db: dbName }],
});
