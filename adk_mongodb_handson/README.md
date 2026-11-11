# MongoDB × Google ADK 技術検証ハンズオン

Google Agent Development Kit (ADK) の MongoDB 統合 (`MongoDbSessionService` / `MongoDbMemoryService` / `MongoDbToolset`) を、
**動かして確かめながら**設計上の特徴と制約を把握するためのハンズオンです。技術記事の執筆やアーキテクチャ選定の材料にすることを想定しています。

- 検証日: 2026-11-11 / 検証環境: Linux (Cloud Agent コンテナ), Docker 29.1.3, Docker Compose 2.40.3, Python 3.12.3
- 外部 API (Gemini / Vertex AI) とクラウドの MongoDB Atlas は**利用できなかった**ため、LLM と Embedding は決定的なテストダブルで代替しています。
  何が実測で、何が未検証かは [11. 検証結果と技術的考察](#11-検証結果と技術的考察) に区別して整理しています。

## 目次

1. [本ハンズオンのポイント](#1-本ハンズオンのポイント)
2. [はじめに](#2-はじめに)
3. [前提知識のおさらい](#3-前提知識のおさらい)
4. [MongoDB × Google ADK の概要](#4-mongodb--google-adk-の概要)
5. [アーキテクチャ](#5-アーキテクチャ)
6. [検証環境の構築](#6-検証環境の構築)
7. [Session Persistence の検証 (Level 1)](#7-session-persistence-の検証-level-1)
8. [Long-term Memory の検証 (Level 2)](#8-long-term-memory-の検証-level-2)
9. [Vector Search / Hybrid Search の検証 (Level 3)](#9-vector-search--hybrid-search-の検証-level-3)
10. [運用・可観測性の検証 (Level 4)](#10-運用可観測性の検証-level-4)
11. [検証結果と技術的考察](#11-検証結果と技術的考察)
12. [クリーンアップ](#12-クリーンアップ)
13. [参考資料](#13-参考資料)

---

## 1. 本ハンズオンのポイント

1. **ソースコードを一次情報にしている**: `google-adk==2.14.0` (2026-11-07 公開) の `google.adk.integrations.mongodb` を直接読み、挙動を Docker 上の実 MongoDB で再現しています。
2. **「MongoDB でできること」と「MongoDB を採用すべき理由」を分離**: 前者は 7〜10 章で事実として検証し、後者は 11 章で設計上のトレードオフとして論じます。
3. **API キー不要で再現可能**: LLM は `ScriptedLlm`、Embedding は `FakeGenaiClient` (ハッシュ化した字句ベクトル) を使います。検索経路・インデックス・競合制御は本物の MongoDB で検証されます。
4. **環境差を明示**: 同じコードが `standalone` / `replicaset` / `atlas-local` でどう変わるか (トランザクション、`$vectorSearch`、`$rankFusion`) を実測しました。
5. **意外な挙動を記録**: 例) 日本語キーワード検索の限界、ベクトル検索に関連度の閾値がないこと、タイムアウトした書き込みが後から確定すること。

## 2. はじめに

### 対象読者

ADK で LLM エージェントを作ったことがあり、本番運用に向けて「会話状態をどこに置くか」「記憶をどう持つか」「検索をどう組み込むか」を検討している方。

### 検証項目

| Level | 対象 | 主な確認事項 |
|---|---|---|
| 1 | `MongoDbSessionService` | 作成/取得、履歴・状態の永続化、再起動後の復元、InMemory との比較、競合制御 |
| 2 | `MongoDbMemoryService` | 記憶登録、別セッションからの検索、キーワード検索、冪等性、ユーザー分離 |
| 3 | `MongoDbToolset` | Vector / Hybrid Search、Embedding 連携、インデックス構築、エージェント応答への利用 |
| 4 | 運用 | 役割分担、複数インスタンス、障害復旧、認証、OpenTelemetry、保存期間、コスト |

### ディレクトリ構成 (`adk_mongodb_handson/`)

```
docker-compose.yml     standalone / replicaset / atlas-local / auth の 4 profile
.env.example           接続先・パスワード・任意の API キー (コミットしない .env の雛形)
requirements.txt       検証したバージョンを固定
handson/               config, fakes (Embedding/LLM のテストダブル), common, search_setup, ops (OTel/削除)
scripts/               level1_session / level2_memory / level3_search / level4_ops
tests/                 pytest (unit は MongoDB 不要、その他はマーカーで分離)
mongo-init/init-auth.js  最小権限ユーザーの作成
```

既存のリポジトリ直下のファイル (FastAPI + MongoDB の User Data 管理アプリ) には一切変更を加えていません。

## 3. 前提知識のおさらい

| 用語 | 意味 |
|---|---|
| Session | 1 つの会話スレッド。イベント (発話・ツール呼び出し・応答) の履歴と、状態 (`state`) を持つ |
| Event | ユーザー発話、モデル応答、ツール呼び出し/結果。`append_event` でセッションに追記される |
| State のスコープ | プレフィックスなし = セッション、`user:` = ユーザー横断、`app:` = アプリ横断、`temp:` = 永続化されない |
| Memory | セッションをまたいで参照できる長期記憶。`add_session_to_memory` で登録、`search_memory` で検索 |
| Runner | エージェント実行の入口。`session_service` / `memory_service` を注入する |
| Toolset | エージェントに渡すツール群。`MongoDbToolset` は `vector_search` と `hybrid_search` を提供 |
| Vector Search | 埋め込みベクトルの近傍検索 (`$vectorSearch`)。検索インデックスが必要 |
| Hybrid Search | 全文検索とベクトル検索を RRF (reciprocal rank fusion) で統合 (`$rankFusion`) |
| 楽観的ロック | 書き込み時にリビジョンを比較し、競合を検出する方式 (ADK では `StaleSessionError`) |

## 4. MongoDB × Google ADK の概要

### 4.1 パッケージと対応バージョン (実測で確認)

| 項目 | 内容 |
|---|---|
| インストール | `pip install "google-adk[mongodb]"` (別パッケージではなく **google-adk 本体**の extra) |
| import | `from google.adk.integrations.mongodb import MongoDbSessionService, MongoDbMemoryService, MongoDbToolset, MongoDbToolSettings` |
| 検証した版 | google-adk **2.14.0**, pymongo **4.19.0** (要件 `>=4.9,<5`), google-genai 2.34.0, opentelemetry 1.45.2 |
| Python | 3.11 以上 (google-adk の要件) |
| 成熟度 | 3 クラスとも `@experimental`。初回利用時に `[EXPERIMENTAL]` 警告が出る。API は変わりうるためバージョンを固定すること |
| 接続 | `connection_string=` (サービスがクライアントを所有) か `mongo_client=` (呼び出し側が所有・共有) のどちらか一方 |

> 注意: PyPI には `adk-mongodb-session` や `google-adk-mongo-session-service` といったコミュニティ製パッケージもありますが、本ハンズオンの対象外 (公式の `google.adk.integrations.mongodb` のみ検証) です。

### 4.2 コンポーネント別の要件 (ソース + 実測)

| コンポーネント | 必要な MongoDB | 備考 |
|---|---|---|
| `MongoDbSessionService` | 任意の MongoDB。**トランザクション**は replica set / sharded / Atlas で有効 | standalone では警告を出して逐次書き込みにフォールバック (実測) |
| `MongoDbMemoryService` keyword (既定) | 任意の MongoDB | 埋め込み・検索インデックス不要 |
| `MongoDbMemoryService` vector | `$vectorSearch` が使える環境 + Google 認証 | Atlas、または自己管理 8.2+ (mongot 同梱構成)。Atlas Local 8.2.11 で実測 |
| `MongoDbMemoryService` auto | Atlas Automated Embedding (Preview) + Voyage | **Atlas Local では不可** (実測: `voyage-4 not registered yet`)。クラウド Atlas のみ・未検証 |
| `MongoDbToolset.vector_search` | `$vectorSearch` + vector index | ソース記載: Atlas 6.0.11+/7.0.2+ または自己管理 8.2+ |
| `MongoDbToolset.hybrid_search` | `$rankFusion` + vector index + 全文 index | ソース記載は「Atlas 8.0+」だが、**Atlas Local 8.0.34 では `$rankFusion` が `QueryFeatureNotAllowed`**、8.2.11 / 9.0.4 で動作 (実測) |

### 4.3 データモデル (コードから)

| コレクション | 内容 |
|---|---|
| `sessions` | 1 セッション 1 文書。`_id={app_name,user_id,session_id}`、`state` (JSON 文字列)、`revision`、`update_time` |
| `events` | 1 イベント 1 文書。`event_data` に JSON 化したイベント全体。索引 `app_user_session_ts` |
| `app_states` / `user_states` | `app:` / `user:` スコープの状態 |
| `memories` | 1 イベント 1 文書。`_id={app,user,session,event_id}`、`text`、`keywords`、(vector 時) `embedding`、(TTL 時) `expire_at` |

状態は JSON 文字列で保存されるため、`.` や `$` を含むキーも安全に往復します。

### 4.4 「MongoDB で実現できること」(事実)

- セッション履歴と状態の永続化、プロセス/インスタンスをまたいだ共有 (Level 1)
- 楽観的ロックによる同一セッションへの同時書き込みの検出 (Level 1)
- 長期記憶のキーワード検索 (追加インフラ不要)、`user_id` 単位の分離、TTL による自動削除 (Level 2)
- ベクトル / ハイブリッド検索をツールとしてエージェントに公開 (Level 3)

「採用すべき理由」は 11 章で扱います。

## 5. アーキテクチャ

```
 ┌──────────────┐   ┌──────────────┐          ┌─────────────────────────────────┐
 │ Agent Runtime│   │ Agent Runtime│  ...     │ MongoDB (replica set / Atlas)   │
 │ (stateless)  │   │ (stateless)  │          │  sessions / events / *_states   │ ← SessionService
 │ Runner       │   │ Runner       │ ───────▶ │  memories (+ vector index)      │ ← MemoryService
 │  + LLM API   │   │  + LLM API   │          │  任意の業務コレクション (+index)  │ ← MongoDbToolset
 └──────────────┘   └──────────────┘          └─────────────────────────────────┘
       ▲  ロードバランサ (セッションアフィニティ不要)
```

### 役割分担

| 責務 | Agent Runtime | MongoDB |
|---|---|---|
| LLM 呼び出し・ツール実行・オーケストレーション | ✅ | |
| 会話履歴・状態の正本 | | ✅ (`sessions` / `events`) |
| 長期記憶 | 登録タイミングの制御 | ✅ (`memories`) |
| 競合検出 | リトライ判断 | ✅ (revision 比較 / トランザクション) |
| 検索インデックス | クエリ生成 | ✅ |
| 認証 / 保存期間 | 接続情報の保持 | ✅ (ロール / TTL) |

Runtime をステートレスに保てるのは、ADK の `Runner` が毎ターン `get_session` で履歴を復元するためです。

### 本ハンズオンの構成

| 目的 | profile | 既定ポート | イメージ |
|---|---|---|---|
| トランザクション非対応時の挙動 | `standalone` | 27117 | `mongo:8.0` (8.0.34) |
| 推奨構成のセッション/記憶 | `replicaset` | 27118 | `mongo:8.0` + `--replSet rs0` |
| Vector / Hybrid Search | `atlas-local` | 27119 | `mongodb/mongodb-atlas-local:8.2` (8.2.11) |
| 認証・最小権限 | `auth` | 27120 | `mongo:8.0` + ユーザー作成スクリプト |

既存の `start_mongodb.sh` が使う 27017 と衝突しないようポートをずらしています。

## 6. 検証環境の構築

### 6.1 手順

```bash
cd adk_mongodb_handson
python3 -m venv .venv-adk && source .venv-adk/bin/activate
pip install -r requirements.txt

cp .env.example .env
# auth profile を使う場合のみ、.env の *_PASSWORD に任意のローカル用パスワードを設定する

docker compose --profile replicaset --profile atlas-local up -d --wait
# 全部試す場合: docker compose --profile standalone --profile replicaset --profile atlas-local --profile auth up -d --wait
```

### 6.2 期待される結果と確認方法

```bash
docker compose --profile replicaset --profile atlas-local ps
# mongo-rs / mongo-atlas-local が (healthy)

python -m pytest tests/test_unit.py -q        # MongoDB 不要。19 passed
```

`mongo-rs` は healthcheck の中で `rs.initiate` を実行するため、初回は healthy になるまで十数秒かかります。

### 6.3 トラブルシューティング

| 症状 | 原因 / 対処 |
|---|---|
| `docker: unknown command: docker compose` | Compose v2 プラグインが未導入。`apt install docker-compose-v2` など |
| `unrecognised option '--replset'` | 正しくは `--replSet` (大文字 S) |
| `Cannot connect to the Docker daemon` | デーモン未起動。`dockerd` を起動 |
| 接続が `ServerSelectionTimeoutError` | 単一ノード RS の host は `localhost:27017` で初期化されるため、URI に `directConnection=true` が必要 (`.env.example` の既定値に含まれる) |
| ポート競合 | `.env` の `MONGO_*_PORT` と `MONGODB_URI_*` を合わせて変更 |
| `.env に MONGO_ROOT_PASSWORD を設定してください` | auth profile は必須変数が空だと起動しない (ハードコードを避けるための仕様) |

## 7. Session Persistence の検証 (Level 1)

### 7.1 なぜセッションの永続化が必要か

- ADK 既定の `InMemorySessionService` はプロセス内の辞書です。**再起動・デプロイ・クラッシュで会話が消え**、複数インスタンスで共有できません (実測: 新しいインスタンスで `get_session` が `None`)。
- ステートレスな Runtime を水平スケールさせるには、会話の正本を外部ストアに置く必要があります。
- 永続化しても API は同じです。`session_service` を差し替えるだけで、`Runner` や Agent のコードは変わりません。

### 7.2 実行

```bash
python -m scripts.level1_session demo
```

期待される出力 (実測):

```
1) 別インスタンスから復元: ['hello'] {'k': 1}
2) 競合制御 (8 writer が同じリビジョンから同時追記)
   リトライなし: {'succeeded': 1, 'stale': 7}
   再取得リトライ: {'succeeded': 8, 'stale': 0}
3) InMemory は新インスタンスで消える: None
```

**プロセス再起動後の復元** (別プロセスで実行):

```bash
python -m scripts.level1_session write --db adk_l1_restart --session-id s-restart
python -m scripts.level1_session read  --db adk_l1_restart --session-id s-restart
# [read] events=['北海道旅行の計画を立てたい', 'いつ頃の予定ですか?']
# [read] state={'topic': 'hokkaido', 'user:language': 'ja', 'app:version': 1}
```

### 7.3 確認できたこと

| 検証項目 | 結果 | 確認テスト |
|---|---|---|
| 作成・取得 | イベントと `session` / `user:` / `app:` 状態が往復する | `test_create_and_get_session_with_events_and_state` |
| 重複 / 不在 | 同一 ID の再作成は `AlreadyExistsError`、不在は `None` | `test_duplicate_session_id_and_missing_session` |
| 状態スコープ | `user:` は同一ユーザーの別セッションに、`app:` は別ユーザーにも見える | `test_state_scopes_...` |
| 再起動後の復元 | 同一プロセス内の再生成 + **実プロセス分離**の両方で復元 | `test_restore_after_...` |
| InMemory との比較 | 同一シナリオで同じイベント列。違いは耐久性のみ | `test_in_memory_session_service_...` |
| 競合制御 | 古いコピーでの追記は `StaleSessionError`。上書きされない | `test_stale_session_is_rejected_...` |
| 同時書き込み | 8 writer・2 インスタンスで勝者 1、失敗 7、**イベント欠損なし** | `test_concurrent_writers_without_retry_...` |
| リトライ | 再取得して再追記すれば全員成功 (`handson.common.append_with_retry`) | `test_concurrent_writers_with_reload_and_retry_...` |
| 件数制限 | `GetSessionConfig(num_recent_events=N)` で直近 N 件、時系列順を維持 | `test_get_session_config_...` |
| 削除 | `delete_session` はイベントも削除 | `test_list_and_delete_session_...` |
| 索引 | 初回利用時に `app_user_session_ts` / `app_user` を自動作成 | `test_recommended_indexes_are_created` |

### 7.4 競合制御の仕組み (ソースより)

`append_event` は (1) セッション文書の `revision` と、呼び出し側が持つ `_storage_update_marker` を比較し、不一致なら `StaleSessionError`、(2) `{_id, revision: 現在値}` 条件付きの `find_one_and_update` で `revision` を +1、(3) イベントを挿入、を **1 つのトランザクション**で行います。同一プロセス内は `asyncio.Lock` で直列化されます。

重要: **ADK は `StaleSessionError` を自動リトライしません**。再取得して再試行するのはアプリ側の責務です。Runner 経由では 1 セッションに対する同時リクエストをアプリ層で直列化する (ユーザー単位のキューなど) か、リトライを実装する必要があります。

### 7.5 環境差 (standalone と replica set)

```bash
docker compose --profile standalone up -d --wait
python -m pytest tests/test_level1_session.py -q -k standalone
```

standalone では `MongoDB deployment does not support transactions ... writes will not be atomic across documents.` の警告が出て逐次書き込みに切り替わります (実測、`IllegalOperation` code 20 を検知して以後キャッシュ)。revision 条件付き更新は効くため競合検出自体は動作しますが、**セッション更新・イベント挿入・共有状態の更新が原子的ではありません**。本番は replica set か Atlas を使ってください。

### 7.6 トラブルシューティング

| 症状 | 対処 |
|---|---|
| `StaleSessionError` が頻発 | 同一セッションへの並行リクエストが原因。再取得リトライまたは直列化 |
| 警告 `could not create the recommended index` | `createIndex` 権限がない。管理者が索引を作成し `ensure_indexes=False` |
| `AlreadyExistsError: Event ... already exists` | 同じ `Event` オブジェクト (同 ID) を二重に追記している |
| `get_session` が遅い | 既定は**全イベント**を読む。`num_recent_events` を指定 (10.5 の実測) |

## 8. Long-term Memory の検証 (Level 2)

### 8.1 セッションと長期記憶の違い

| 観点 | Session | Memory |
|---|---|---|
| 目的 | 1 会話の文脈 (履歴・状態) を正確に再現 | 過去の会話から**関連する断片**を思い出す |
| 単位 | セッション ID で一意に取得 | `user_id` 単位で検索 (セッションをまたぐ) |
| 取得方法 | キー (`app/user/session`) による完全な読み出し | クエリによる**検索** (結果は部分的・順位付き) |
| 一貫性 | 厳密 (revision による競合検出) | 緩い (冪等な upsert、検索は近似) |
| 保存期間 | 会話の寿命 (終了後に削除可) | 長期 (TTL で期限管理) |
| ADK の API | `BaseSessionService` | `BaseMemoryService` (`add_session_to_memory` / `search_memory`) |

### 8.2 実行

```bash
python -m scripts.level2_memory
```

```
1) 登録件数 (2 回登録): 3
2) 'hokkaido': ['Hokkaido has great trails in Daisetsuzan', 'I want to go hiking in Hokkaido next summer']
3) 'MongoDB' (日本語混在): ['私はMongoDBが好きです']
4) '好き' (部分一致なし): []
5) 別ユーザー bob: []
```

### 8.3 確認できたこと

| 検証項目 | 結果 |
|---|---|
| セッションからの登録 | テキストを持つイベントのみ 1 イベント 1 文書で保存。思考 (`thought`) パートと非テキストイベントは除外 |
| 別セッションからの検索 | `search_memory(app_name, user_id, query)` は `session_id` を取らず、ユーザー単位で検索される |
| 冪等性 | `_id = (app, user, session, event_id)` の `ReplaceOne(upsert)`。3 回登録しても件数は変わらない。別セッションは別文書 |
| ユーザー/アプリ分離 | クエリに `app_name` と `user_id` が必ず入る。別ユーザー・別アプリには 0 件 |
| ランキング | キーワードの重なり数 → 新しい順。`max_results` (既定 50) で打ち切り |
| インデックス | `app_user_keywords` (複合 multikey)。`memory_ttl_days` 指定時は `memory_ttl` (TTL) も作成 |

### 8.4 キーワード検索の挙動と限界 (実測)

- クエリ・文書とも `\w+` で分割して小文字化し、短い英語ストップワードを除去、**キーワードの集合が交差**する文書を返します。
- 大文字小文字は無視されます (`HOKKAIDO` でヒット)。
- **日本語は形態素解析されません。** 空白区切りの `天気` はヒットしますが、`私はMongoDBが好きです` 中の `好き` はヒットしません。ラテン文字の連続 (`MongoDB`) は切り出されるためヒットします。
- 同義語・言い換えは拾えません (`trekking` は `hiking` にヒットしない)。
- 英語ストップワードのみのクエリは空結果。
- 日本語中心のサービスでは、`stop_words` の調整だけでは足りず、(a) 前処理で分かち書きして登録/検索する、(b) vector モードにする、のいずれかが必要です。

### 8.5 トラブルシューティング

| 症状 | 対処 |
|---|---|
| 記憶が見つからない | 登録時と検索時の `app_name` / `user_id` / `retrieval_mode` が一致しているか。**モード違いの記憶は互いに見えない** (keyword で登録した記憶は vector で見えない: `test_memory_vector_mode_roundtrip_...`) |
| 登録しても 0 件 | イベントにテキストがない、またはストップワードのみ |
| 記憶がいつまでも消えない | TTL は `memory_ttl_days` 指定時のみ。削除は約 1 分周期のバックグラウンド (10.6) |

## 9. Vector Search / Hybrid Search の検証 (Level 3)

> 必要な環境: `$vectorSearch` が使える MongoDB (Atlas / Atlas Local / 自己管理 8.2+ の mongot 構成)。**通常の `mongo:8.0` ではツールが `{"status": "ERROR", ...}` を返す**ことを実測しています (`test_vector_search_is_unavailable_on_plain_mongod`)。

### 9.1 長期記憶にベクトル検索が必須ではない理由

- keyword モードは**追加インフラ・追加コスト・外部 API なし**で動き、固有名詞・ID・コマンド名・型番のような**語彙が一致する想起**には十分です。
- ベクトル検索は埋め込み API の呼び出し (登録時と検索時の両方)、検索インデックス (別プロセス `mongot`)、次元数/モデルの固定という運用負荷を伴います。モデルを変えると既存ベクトルが無効になり、**再埋め込みが必要**です。
- ADK は 3 モード (`keyword` / `vector` / `auto`) を選べるため、「まず keyword で始め、再現率が不足した時点で vector へ」という段階導入ができます。ただし**モード間で記憶は引き継がれない**ため、切り替え時は再登録が必要です。
- 一方、言い換え・多言語・曖昧な質問への想起が要件なら vector が必要です (日本語は 8.4 の通り keyword が弱い)。**必須かどうかは要件次第**であり、既定が keyword であることがその設計判断を表しています。

### 9.2 実行

```bash
docker compose --profile atlas-local up -d --wait
python -m scripts.level3_search
```

実測出力 (抜粋):

```
index queryable まで 1.1s
vector {"status": "SUCCESS", "rows": [{"name": "Robot Vacuum", "search_score": 0.77}, {"name": "Steam Mop", "search_score": 0.5}, ...]}
vector+filter(outdoor) {"status": "SUCCESS", "rows": [{"name": "Hiking Boots", ...}, {"name": "Camping Stove", ...}, ...]}
hybrid {"status": "SUCCESS", "rows": [{"name": "Robot Vacuum", "search_score": 0.0328}, {"name": "Steam Mop", "search_score": 0.0161}, ...]}
agent answer: search results: Robot Vacuum, Steam Mop
```

### 9.3 インデックスの構築

`handson/search_setup.py` が次の 2 つを作成します (`create_search_index` は**非同期**で、`queryable` になるまで検索は使えません)。

```python
# ベクトルインデックス: 埋め込みの次元・類似度と、事前フィルタに使うフィールド
{"type": "vectorSearch", "name": "vector_index",
 "definition": {"fields": [
   {"type": "vector", "path": "embedding", "numDimensions": 64, "similarity": "cosine"},
   {"type": "filter", "path": "category"}]}}
# 全文インデックス (hybrid の text 側)。名前は既定で "default"
{"type": "search", "name": "default",
 "definition": {"mappings": {"dynamic": False, "fields": {"description": {"type": "string"}}}}}
```

実測 (Atlas Local): `queryable` まで 1〜6 秒。

> ⚠️ 落とし穴: **同名 DB を `drop_database` した直後に同名で索引を作り直すと、`queryable` が 60 秒以上ならなかった**事例がありました (Atlas Local 8.0)。ハンズオンでは検証ごとに一意な DB 名 (`unique_name`) を使っています。

### 9.4 Embedding との連携

| 項目 | 内容 |
|---|---|
| クエリの埋め込み | `MongoDbToolSettings.vertex_ai_embedding_model_name` (既定 `text-embedding-005`) を genai Client 経由で呼び、`queryVector` として送る |
| 文書側の埋め込み | **アプリ側の責務** (`embedding` フィールドに保存)。クエリと**同一モデル・同一次元**が必須 |
| 次元 | 既定は 768。異なる場合は `vertex_ai_embedding_output_dimensionality` と索引の `numDimensions` を揃える |
| 認証 | genai Client の既定 (`GOOGLE_API_KEY` または Vertex AI)。`genai_client=` で差し替え可能 (本ハンズオンは `FakeGenaiClient`) |
| Automated Embedding (Preview) | `use_mongodb_auto_embedding=True`。Atlas が Voyage AI でクエリと文書を埋め込む。**Atlas Local では不可** (実測) |

### 9.4.1 テストダブルの意味

`FakeGenaiClient` はトークンのハッシュ化による字句ベースのベクトルを返すだけです。したがって以下は検証**できていません**: 同義語・言い換えの順位、多言語、実モデルの次元/レイテンシ/課金、検索品質。検証できたのは「クエリ→埋め込み→`$vectorSearch`→結果」の経路、フィルタ、ランキング形式、エラー処理です。

### 9.5 確認できたこと

| 項目 | 結果 |
|---|---|
| Vector Search | 関連商品が最上位。`search_score` 降順。埋め込みフィールドは既定で結果から除外 |
| **関連度の閾値なし** | 無関係なクエリでも最近傍が `limit` 件返る (無関係でも score 0.5 = 直交)。足切りはアプリ側で実装 |
| 事前フィルタ | `filter={"category": "outdoor"}` で候補を限定。`type: filter` が未定義のフィールドは `ERROR` |
| `limit` 上限 | `MongoDbToolSettings.max_results` (既定 50) でモデルの指定値を制限 |
| Hybrid Search | `$rankFusion` (RRF)。スコアは cosine ではなく **RRF 値 (~0.03)** でスケールが違う |
| 重み | `vector_weight=0` で全文一致が上位、vector のみの候補は score 0 で末尾に残る |
| フィルタの安全性 | `$where` / `$function` / `$expr` など許可リスト外の演算子は DB に触れる前に `ERROR` (`test_search_tool_rejects_unsafe_filter_operators_...`) |
| エラー処理 | 例外ではなく `{"status": "ERROR", "error_details": ...}` を返す。LLM が結果を見て再試行できる |
| 記憶の vector モード | `MongoDbMemoryService(retrieval_mode="vector", ...)` で登録/検索、`app_name`+`user_id` の事前フィルタでユーザー分離 |

### 9.6 検索結果をエージェントの応答に使う方法

1. `Agent(tools=[MongoDbToolset(...)])` を渡す。モデルに見えるのは `mongodb_vector_search` / `mongodb_hybrid_search` (Agent 側で `mongodb_` が付く) と検索パラメータだけで、**クライアント・DB 名・設定・genai Client はモデルから隠される** (`test_toolset_exposes_two_tools_...`)。
2. モデルがツールを呼ぶと `{"status": "SUCCESS", "rows": [...]}` が `function_response` としてイベント履歴に入り、次のモデル呼び出しで文脈になる。
3. 本ハンズオンの `ScriptedLlm` は「ツール呼び出し → 結果の `name` を連結して回答」を決定的に再現し、**ツール呼び出しも会話履歴として MongoDB に永続化される**ことをテストで確認しています (`test_agent_uses_search_results_in_answer_and_persists_conversation`)。
4. 実 LLM (`gemini-*`) を使う場合は `python -m scripts.level3_search --live` (未検証。9.7 参照)。

**セキュリティ上の注意**: モデルは**コレクション名・インデックス名・返却フィールドを選べます**。`MongoDbToolset` は 1 つの DB に束縛されるため、**エージェントに読ませてよいデータだけの DB を割り当てる**か、`before_tool_callback` でコレクションを制限してください (`tool_filter` はツール単位でしか効きません。ソースの docstring 記載)。さらに DB ユーザーは `read` ロールに限定します (10.4)。

### 9.7 未検証

- 実 Embedding (`text-embedding-005` 等) での検索品質、`--live` オプション
- 実 LLM がツールを選択・引数を生成する挙動
- Atlas クラウド (M0 など) での動作、Automated Embedding (`auto` モード / `use_mongodb_auto_embedding`)
- 自己管理 MongoDB 8.2+ Community + mongot の個別構成 (Atlas Local は mongot を同梱した別構成)

### 9.8 トラブルシューティング

| 症状 | 原因 / 対処 |
|---|---|
| `Using $search and $vectorSearch ... requires additional configuration` | 通常の mongod に接続している。Atlas / Atlas Local を使う |
| `$rankFusion is not allowed ... feature flag` | 8.0 系の Atlas Local。`mongodb-atlas-local:8.2` 以降を使う |
| 結果が空配列 | 索引が未 `queryable`、`embedding` の次元不一致、`index_name` / `embedding_field` の指定違い |
| `Vector index ... does not match this service's configuration` | 既存索引と `numDimensions` / `type` が異なる。索引を作り直すか `vector_index_name` を変える |
| `voyage-4 not registered yet` | `auto` モードは Atlas Local 非対応 |

## 10. 運用・可観測性の検証 (Level 4)

凡例: 🔬 = 実測、📝 = 設計上の考察 (未実測)。

### 10.1 ステートレスな Agent Runtime と MongoDB の役割分担

📝 5 章の表を参照。判断基準は「プロセスが突然消えても会話が続けられるか」。Runtime はキャッシュとして `Session` オブジェクトを持ちますが、`append_event` のたびに revision が進むため、**古いコピーは使い捨て**として扱います。

### 10.2 複数インスタンスでのセッション共有

🔬 `MongoDbSessionService` を 2 インスタンス作り (別プロセス相当)、同一セッションを共有:

```bash
python -m pytest tests/test_level4_ops.py -q -k multi_instance
```

インスタンス A がターン 1 を処理して**消滅**し、インスタンス B がターン 2 を処理しても、履歴は `['from A', 'echo: from A', 'from B', 'echo: from B']` と連続します。同時書き込みは 7.3 の通り 1 勝者 + `StaleSessionError`。

📝 ロードバランサでのセッションアフィニティは不要ですが、同一セッションへの並行リクエストはリトライ/直列化が必要です。

### 10.3 障害発生時の復旧動作

```bash
python -m scripts.level4_ops failure          # MongoDB 停止 → 書き込み → 再起動
python -m scripts.level4_ops failure 5000     # 5 秒でタイムアウトさせる
```

🔬 実測 (replicaset, 単一ノード):

| 条件 | 結果 |
|---|---|
| 停止中に `append_event` (既定設定) | **約 121 秒後**に `NetworkTimeout` (トランザクションの再試行ループ) |
| `timeoutMS=5000` を MongoClient に指定 | **約 124 秒**で失敗。**この経路では短縮されなかった** |
| アプリ側で `asyncio.wait_for(5s)` | 5 秒で `TimeoutError`。ただしスレッドは継続し、**再起動後にその書き込みが確定した** (ack 済み 2 件に対し保存 3 件) |
| 再起動後の復帰 | **約 2 秒**で、同じ `MongoClient` のまま (プロセス再起動なし) 読み書き再開 |
| ack 済み書き込みの欠損 | なし |

示唆:

- 停止時にリクエストが 2 分ハングしうるため、**ロードバランサ/HTTP 層のタイムアウトとヘルスチェック (readiness)** で守る必要があります。
- タイムアウトしたリクエストは「**結果不明**」です。リトライ時は先に `get_session` で確定済みかを確認するか、イベント ID を固定して `AlreadyExistsError` を冪等性チェックに使う設計が必要です。
- 📝 本番の replica set (3 ノード) では primary 選出中の書き込み失敗は一時的ですが、これは未実測です。

### 10.4 MongoDB の認証と権限制御

```bash
# .env に auth 用パスワードを設定した上で
docker compose --profile auth up -d --wait
python -m scripts.level4_ops auth
python -m pytest tests/test_level4_ops.py -q -k least_privilege
```

🔬 `mongo-init/init-auth.js` が作る 3 ユーザーでの実測:

| ユーザー | ロール | 結果 |
|---|---|---|
| `adk_app` | 対象 DB の `readWrite` | セッション作成・追記・索引作成まで成功 |
| `adk_reader` | `read` | 書き込みは `Unauthorized (code 13)` で拒否。**検索ツール専用の接続はこのロールにする** |
| `adk_noindex` | find/insert/update/remove のみ | 動作は継続。`could not create the recommended index` 警告のみ (`ensure_indexes=False` で抑止) |
| 誤ったパスワード | | `AuthenticationFailed (code 18)` |

📝 推奨: 本番はセッション用 (`readWrite`) と検索ツール用 (`read`) で**別の接続・別のユーザー**を使う。索引は管理者が事前作成し `ensure_indexes=False`。パスワードは `connection_string_env_var=` で環境変数から解決する (`MongoDbToolset` は Agent Engine に pickle する際、この方式ならパスワードがパッケージに含まれない: `test_toolset_with_env_var_is_picklable_without_credentials`)。TLS・IP 制限・ネットワーク分離は本ハンズオンでは未検証。

### 10.5 OpenTelemetry によるトレース

```bash
python -m scripts.level4_ops otel
```

🔬 実測:

- ADK 自体が `invoke_workflow` / `invoke_agent` / `generate_content` / `execute_tool` の span を出す。属性は `gen_ai.operation.name`, `gen_ai.agent.name`, `gen_ai.tool.name` など。
- **`MongoDbSessionService` の DB 呼び出しは span にならない** (`test_adk_does_not_trace_session_service_calls_by_itself`)。
- そこで `handson.ops.MongoCommandTracer` (pymongo `CommandListener`) を `MongoClient(event_listeners=[...])` に渡すと、DB コマンドが span になる。コマンド本文は会話内容を含むため記録せず、コマンド名・DB 名・所要時間のみ記録。
- アプリ側で親 span (`handle_request`) を作ると、**1 つの trace_id** に ADK の span と DB の span が収まる (`asyncio.to_thread` が contextvars を引き継ぐ)。親 span がないと、ADK が span を開く前の DB 呼び出し (最初の `get_session` など) は別 trace になる。

```
handle_request
  mongodb.find ×4                    ← Runner によるセッション/状態の読み出し
  invoke_workflow agent
    mongodb.find / findAndModify / insert / commitTransaction   ← ユーザー発話の追記
    invoke_agent agent
      generate_content scripted
    mongodb.find / findAndModify / insert / commitTransaction   ← 応答の追記
```

本番エクスポート (OTLP 等) は SDK の `TracerProvider` に任意のエクスポータを付けるだけですが、本ハンズオンではインメモリのみ確認しました (📝 OTLP コレクタ連携は未検証)。

### 10.6 セッションデータの保存期間と削除

```bash
python -m scripts.level4_ops purge   # セッション削除バッチ
python -m scripts.level4_ops ttl     # 記憶 TTL (約 1 分待つ)
```

| 対象 | 仕組み | 結果 |
|---|---|---|
| 記憶 | `memory_ttl_days=N` で `expire_at` (Date) と TTL 索引 `memory_ttl` | 🔬 `expire_at` を過去にして **54 秒後に削除**。TTL モニタは約 60 秒周期なので即時ではない。再登録で期限が更新される |
| セッション | **TTL 非対応**。`update_time` は epoch 秒 (float) で保存され、TTL 索引は Date 型にしか効かない | 🔬 TTL 索引を張っても**削除されない**ことを確認 |
| セッション (代替) | 定期バッチで `update_time` が古いものを削除 (`handson.ops.purge_sessions`)。`delete_session` はイベントも削除 | 🔬 30 日超のセッションとそのイベントのみ削除し、`user:` 状態は残る |
| 状態 | `app_states` / `user_states` はセッション削除では消えない | 🔬 同上 |

📝 個人情報を含む会話の「削除要求」に応えるには、`events` / `memories` / `user_states` の 3 箇所を `user_id` で消す手順が必要です (本ハンズオンの `purge_sessions` は期間指定のみ)。

### 10.7 運用コストとスケーラビリティ

ローカル Docker (単一マシン、1 回の計測でばらつきあり) の参考値。**本番性能の指標ではありません**。

```bash
python -m scripts.level4_ops bench
```

| 指標 (200 イベント, 各 ~210B のテキスト) | InMemory | MongoDB standalone | MongoDB replica set |
|---|---|---|---|
| `append_event` p50 / p95 | 0.03 / 0.06 ms | 2.1 / 4.1 ms | 5.1 / 12.6 ms |
| `get_session` (全 200 件) | 平均 8.3 ms (p95 に 73 ms のスパイク。ディープコピー/GC と推測) | p50 8.1 ms | p50 8.3 ms |
| `get_session` (直近 10 件) 平均 | 未計測 | 未計測 | 約 2.3〜2.7 ms |
| イベント 1 件の保存サイズ (`avgObjSize`) | - | 748 B | 748 B |

- 🔬 1 ターンは複数回の `append_event` (ユーザー発話、ツール呼び出し、ツール結果、応答) と読み出しを伴うため、レイテンシは LLM 呼び出し (数百 ms〜秒) に比べれば小さいものの、**ターンあたり DB 往復が数十回**になりえます。replica set ではトランザクションの分だけ追記が約 2 倍遅い。
- 🔬 `get_session` は既定で全イベントを読みます。履歴が長い会話では**線形に増える**ため、`num_recent_events` で制限する (約 3 倍高速) か、履歴圧縮を検討します。
- 🔬 イベントは `event_data` に完全な JSON を保存するため、テキスト 210B が約 750B になります (約 3.5 倍)。ツール結果 (検索結果の `rows` など) が大きいと急増します。
- 📝 スケール: 読み書きは `app_user_session_ts` 索引で効率化されますが、1 セッション内の追記は revision で直列化されるため、**ホットなセッションへの並行書き込みが律速**になります。セッション数の増加はシャーディングで対応可能 (未検証)。
- 📝 コスト: 追加の DB 運用 (または Atlas 料金)、トランザクションのため replica set 最小 3 ノード、ベクトル検索は検索ノード (mongot) の分が加算。keyword モードなら mongot も埋め込み API も不要。

## 11. 検証結果と技術的考察

### 11.1 実測した項目と未検証項目

| 区分 | 項目 | 状態 |
|---|---|---|
| 実測 | Level 1 全項目 (作成/取得、永続化、プロセス再起動、InMemory 比較、競合) | ✅ standalone / replica set / Atlas Local |
| 実測 | Level 2 全項目 (登録、別セッション検索、keyword、冪等性、ユーザー分離、TTL) | ✅ |
| 実測 | Vector / Hybrid Search、索引構築、記憶 vector モード、エージェント統合 | ✅ Atlas Local 8.2.11 + `FakeGenaiClient` / `ScriptedLlm` |
| 実測 | 障害停止/復旧、認証・権限、OpenTelemetry span、削除、簡易ベンチ | ✅ ローカル Docker |
| **未検証** | 公式ドキュメント/ブログの記述との照合 | ❌ この環境から `mongodb.com` に接続できなかったため、**実装は GitHub / PyPI の公開ソース (google-adk 2.14.0、`src/google/adk/integrations/mongodb/README.md`) とインストール済みコードで照合**。MongoDB 公式ブログ・Atlas ドキュメントの内容は未確認 |
| **未検証** | 実 Gemini / Vertex AI での LLM・Embedding (`--live`) | ❌ API キーなし |
| **未検証** | MongoDB Atlas クラウド、Automated Embedding (`auto`)、Atlas Search の本番挙動 | ❌ |
| **未検証** | 3 ノード replica set の primary 切替、シャーディング、TLS、大規模負荷、OTLP 連携 | ❌ |
| **未検証** | `$rankFusion` が使える自己管理 Community 版 (mongot 構成) | ❌ Atlas Local のみ |
| 考察 | 10 章の 📝 項目、本章の設計判断 | 実測ではなく推論 |

### 11.2 MongoDB に集約するメリットとデメリット

| | メリット | デメリット |
|---|---|---|
| 運用 | データストアが 1 つ。バックアップ・監視・認証・ネットワークを共通化できる | セッション書き込みと検索負荷が**同じクラスタで競合**する。障害の影響範囲が広い (単一障害点) |
| 一貫性 | セッション・状態・記憶・業務データを同じトランザクション境界/同じ ID で扱える | ADK のトランザクションは**セッション系のみ**。記憶や業務データとの原子性は提供されない |
| 開発 | 同一ドライバ (`mongo_client` を共有可)、スキーマレスで ADK のイベント構造変更に強い | 実験的 API (`@experimental`)。イベントが JSON 文字列で保存され、アドホックなクエリや分析がしにくい |
| コスト | 小規模なら追加サービス不要。keyword なら mongot も不要 | ベクトル検索を使うと検索ノード分の費用。履歴が肥大化しやすい (10.7) |
| ガバナンス | ロール・TTL・監査を一元化 | セッション TTL がなく自前バッチが必要。**ツールが読める範囲の設計** (DB 単位) に注意 |

### 11.3 専用ベクトル DB を使う構成との違い

| 観点 | MongoDB に集約 | 専用ベクトル DB (+ 別の会話ストア) |
|---|---|---|
| ベクトルとメタデータ | 同一文書。`type: filter` による事前フィルタで、業務データと同じ ACL/更新で扱える | 別系統。同期 (二重書き込み/CDC) の設計・遅延・不整合の管理が必要 |
| 運用対象 | 1 システム | 2 システム以上 |
| ADK との統合 | 公式の `MongoDbToolset` / `MongoDbMemoryService` | 公式統合の有無はベクトル DB による。自前ツールが必要になることが多い |
| 検索の専門性 | ハイブリッド (RRF)・事前フィルタが標準。大規模・高 QPS・特殊な ANN チューニングは専用 DB が有利な場合がある (📝 未比較) | 規模や機能に特化 |
| ロックイン | MongoDB 固有の `$vectorSearch` / `$rankFusion` | DB ごとに異なる |

本ハンズオンでは専用ベクトル DB との性能比較は**行っていません**。選定は、データ量・QPS・既存の MongoDB 運用の有無で判断してください。

### 11.4 MongoDB Atlas とセルフマネージドの違い

| 観点 | Atlas | セルフマネージド |
|---|---|---|
| トランザクション (replica set) | 標準で満たされる | 自分で replica set を構成 (standalone は非原子的になる: 7.5) |
| ベクトル / ハイブリッド検索 | `$vectorSearch` / `$rankFusion` 対応 (Atlas) | 8.2+ かつ mongot が必要。Community/Enterprise の構成差・導入の手間 (📝) |
| Automated Embedding (Preview) | 利用可能 (Voyage) | 8.3+ / mongot 1.70.1+ / Voyage キーを自分で設定 (README 記載) |
| 運用 (バックアップ・監視・認証・TLS) | マネージド | 自前 |
| コスト | 従量/専有クラスタ料金 | インフラ + 運用工数 |
| 検証状況 | 本ハンズオンでは **Atlas Local で代替**。クラウドは未検証 | 通常 mongod (standalone / RS) を実測 |

### 11.4.1 判断の目安

- **Atlas**: ベクトル/ハイブリッド検索や Automated Embedding を使いたい、運用を減らしたい。
- **セルフマネージド**: データ所在地・コスト・既存基盤の制約があり、keyword 記憶 + セッション永続化が主目的 (replica set で十分)。

### 11.5 「MongoDB を採用すべき理由」(判断材料)

4.4 の「できること」とは別に、次の条件に当てはまるほど採用理由が強くなります。

1. すでに MongoDB を運用している / 業務データが MongoDB にある (検索ツールを同一 DB に当てられる)。
2. Runtime を水平スケール・オートスケールさせたい (InMemory では不可)。
3. ドキュメント指向のイベント構造をそのまま保存したい。
4. 記憶を段階的に高度化したい (keyword → vector → auto を同一基盤で)。

逆に、(a) 会話状態を Redis 等の低レイテンシ KV で十分に扱える、(b) 既存の SQL 基盤に統一したい (ADK 標準の DB/Vertex AI セッションサービスがある)、(c) 実験的 API を避けたい、といった場合は他の選択肢が優位になりえます。

### 11.6 実装上の注意 (まとめ)

- バージョンを固定する (`@experimental`)。
- 本番は replica set / Atlas。standalone はトランザクションなし。
- `StaleSessionError` のリトライと、タイムアウト時の「結果不明」を設計する。
- `get_session` には `num_recent_events` を使う。
- 日本語の keyword 検索は限界がある (分かち書き前処理か vector)。
- ベクトル検索は関連度の閾値がない。検索ツールには `read` ロールと専用 DB を割り当てる。
- 索引は `queryable` になるまで待つ。モードやモデルを変えたら再登録 / 再埋め込み。

## 12. クリーンアップ

```bash
cd adk_mongodb_handson
docker compose --profile standalone --profile replicaset --profile atlas-local --profile auth down -v   # コンテナ + ボリューム削除
deactivate && rm -rf .venv-adk .env
# 必要に応じてイメージも削除
docker rmi mongo:8.0 mongodb/mongodb-atlas-local:8.2
```

確認: `docker compose ps -a` が空であること。スクリプトは各自一意な DB を作成し、終了時に削除します。

## テストの実行

| コマンド | 内容 | 必要なもの |
|---|---|---|
| `python -m pytest tests/test_unit.py` | ユニット (19) | なし (Python のみ) |
| `python -m pytest tests -m "not slow and not live"` | 全統合テスト | 全 profile 起動 (接続できないテストは skip) |
| `REQUIRE_MONGO=1 python -m pytest tests -m "not slow and not live"` | 接続不可を失敗として扱う (CI) | 全 profile 起動 |
| `python -m pytest tests -m slow` | TTL モニタ待ち (約 1 分) | replicaset |
| `python -m pytest tests -m "atlas_local"` | Level 3 のみ | atlas-local |

マーカー: `standalone` / `replicaset` / `atlas_local` / `auth` / `slow` / `live` (外部 API。現状 `live` のテストは未作成で、通常実行に含まれない)。
GitHub Actions: `.github/workflows/adk-mongodb-handson.yml` (`unit` ジョブ = MongoDB 不要、`integration` ジョブ = Docker Compose で 4 profile を起動。パスワードは実行時に生成)。

### 検証実績 (2026-11-11, ローカル Docker)

- `pytest tests -m "not slow and not live"`: **60 passed**、`pytest tests -m slow`: **1 passed** (TTL)
- ユニットのみ (MongoDB なし): **19 passed**

## 13. 参考資料

- MongoDB 公式ブログ: <https://www.mongodb.com/company/blog/product-release-announcements/introducing-mongodb-google-adk-integration-durable-memory-agent-state> (※この環境から接続できず未確認)
- MongoDB Atlas ドキュメント: <https://www.mongodb.com/docs/atlas/ai-integrations/google-adk/> (※同上)
- google/adk-python: <https://github.com/google/adk-python> (本ハンズオンの根拠: `src/google/adk/integrations/mongodb/` と同ディレクトリの `README.md`)
- google-adk (PyPI, 2.14.0): <https://pypi.org/project/google-adk/>
- Atlas Vector Search `$vectorSearch`: <https://www.mongodb.com/docs/vector-search/query/aggregation-stages/vector-search-stage/> (ソース内の docstring が参照)
- Atlas Local (Docker): <https://hub.docker.com/r/mongodb/mongodb-atlas-local>
- OpenTelemetry Python: <https://opentelemetry.io/docs/languages/python/>
