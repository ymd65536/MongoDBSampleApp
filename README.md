# User Data Management System

MongoDBを使用したユーザーデータ管理システムです。バックエンドAPI（FastAPI）とWebフロントエンドを提供します## 🔧 詳細な使用方法

### Webアプリケーション機能

#### 1. フロントエンド操作
- **ユーザー追加**: 商品名と価格を入力して新規データを作成
- **データ表示**: 登録されている全商品をカード形式で表示
- **データ編集**: 各商品の編集ボタンから商品名・価格を変更
- **データ削除**: 削除ボタンで不要なデータを削除（確認ダイアログ付き）
- **リアルタイム更新**: データ変更後の自動リロード
- **ヘルスチェック**: MongoDB接続状態をリアルタイム監視

#### 2. API操作（開発者向け）
```bash
# 全ユーザー取得
curl http://localhost:8000/api/users

# 特定ユーザー取得
curl http://localhost:8000/api/users/1

# 新規ユーザー作成
curl -X POST http://localhost:8000/api/users \
  -H "Content-Type: application/json" \
  -d '{"product": "新商品", "price": 5000.0}'

# ユーザー更新
curl -X PUT http://localhost:8000/api/users/1 \
  -H "Content-Type: application/json" \
  -d '{"product": "更新された商品", "price": 6000.0}'

# ユーザー削除
curl -X DELETE http://localhost:8000/api/users/1

# ヘルスチェック
curl http://localhost:8000/api/health
```

### 従来の方法（コマンドライン）

#### 1. JSONファイル作成とMongoDB投入の一括実行
```bash
.venv/bin/python main.py
```

#### 2. 既存のJSONファイルのみをMongoDBに投入
```bash
.venv/bin/python mongodb_uploader.py
```件

- Python 3.7+
- MongoDB サーバー
- pymongo パッケージ

## 🚀 セットアップ

### 1. Pythonパッケージのインストール
```bash
pip install pymongo
```

### 2. MongoDBサーバーの起動

#### Homebrewを使用している場合：
```bash
# MongoDBをインストール（初回のみ）
brew tap mongodb/brew
brew install mongodb-community

# MongoDBサーバーを起動
brew services start mongodb-community

# または提供されているスクリプトを使用
./start_mongodb.sh
```

#### Dockerを使用する場合：
```bash
# MongoDBコンテナを起動
docker run --name mongodb-local -d -p 27017:27017 --rm mongo:latest
```

## 📁 ファイル構成

```
ld_json/
├── main.py                    # サンプルデータ生成スクリプト
├── mongodb_uploader.py        # MongoDB管理クラス
├── api.py                     # FastAPI バックエンド
├── test_mongodb_connection.py # 接続テスト
├── setup.sh                  # セットアップスクリプト
├── start_mongodb.sh          # MongoDB起動スクリプト  
├── start_api.sh              # APIサーバー起動スクリプト
├── requirements.txt          # 依存関係
├── static/
│   └── index.html            # フロントエンドUI
├── user_data_files/          # JSONファイルストレージ
│   ├── user_1.json
│   ├── user_2.json
│   ├── user_3.json
│   └── ... (12ファイル)
└── README.md
```

## � クイックスタート（推奨）

### 1️⃣ セットアップ
```bash
# 自動セットアップを実行
./setup.sh
```

### 2️⃣ MongoDB起動
```bash
# MongoDBサーバーを起動
./start_mongodb.sh

# または手動で起動
brew services start mongodb-community
```

### 3️⃣ サンプルデータ作成（初回のみ）
```bash
# JSONファイルを作成してMongoDBにアップロード
.venv/bin/python main.py
```

### 4️⃣ Webアプリケーション起動
```bash
# APIサーバーとフロントエンドを起動
./start_api.sh
```

### 5️⃣ ブラウザでアクセス
- **Web管理画面**: http://localhost:8000
- **APIドキュメント**: http://localhost:8000/docs
- **健康状態確認**: http://localhost:8000/api/health

## �🔧 詳細な使用方法

### 基本的な使用方法

1. **JSONファイル作成とMongoDB投入の一括実行：**
```bash
python main.py
```

2. **既存のJSONファイルのみをMongoDBに投入：**
```bash
python mongodb_uploader.py
```

#### 3. カスタム設定での使用

```python
from mongodb_uploader import MongoDBUploader

# カスタム接続設定
uploader = MongoDBUploader(
    connection_string="mongodb://localhost:27017/",
    database_name="my_custom_db"
)

if uploader.connect():
    # 特定のディレクトリとコレクション名を指定
    uploader.upload_json_files(
        data_dir="custom_data_dir",
        collection_name="custom_collection"
    )
    
    # データの確認
    uploader.show_collection_data("custom_collection")
    
uploader.disconnect()
```

## 🔄 サーバー管理

### APIサーバーの起動・停止
```bash
# 起動
./start_api.sh

# 手動起動（開発モード）
.venv/bin/python -m uvicorn api:app --host 0.0.0.0 --port 8000 --reload

# 停止
# Ctrl+C でサーバーを停止

# バックグラウンド起動
nohup .venv/bin/python -m uvicorn api:app --host 0.0.0.0 --port 8000 &

# プロセス確認
ps aux | grep uvicorn

# プロセス停止
pkill -f uvicorn
```

### MongoDB管理
```bash
# MongoDB起動
./start_mongodb.sh
# または
brew services start mongodb-community

# MongoDB停止
brew services stop mongodb-community

# MongoDB状態確認
brew services list | grep mongodb

# MongoDB接続テスト
.venv/bin/python test_mongodb_connection.py
```

## 🗄️ MongoDB設定

### デフォルト設定
- **接続先:** `mongodb://localhost:27017/`
- **データベース名:** `user_data_db`
- **コレクション名:** `users`

### データ構造
各JSONファイルは以下の追加情報と共にMongoDBに保存されます：

```json
{
  "id": 1,
  "product": "MacBook Pro 14インチ",
  "price": 298000.0,
  "source_file": "user_1.json",        // 追加：ソースファイル名
  "uploaded_at": "2025-10-01T...",     // 追加：アップロード日時
  "_id": ObjectId("...")               // MongoDB自動生成ID
}
```

### 現在のサンプルデータ
| ID | 商品名 | 価格 |
|----|--------|------|
| 1 | MacBook Pro 14インチ | ¥298,000 |
| 2 | ワイヤレスマウス（Logitech MX Master 3） | ¥12,800 |
| 3 | 4K外部モニター 27インチ（Dell UltraSharp） | ¥89,800 |
| 4 | メカニカルキーボード（HHKB Professional HYBRID） | ¥36,300 |
| 5 | iPad Pro 12.9インチ（Wi-Fi + Cellular） | ¥158,800 |
| 6 | iPhone 15 Pro Max 256GB | ¥164,800 |
| 7 | AirPods Pro（第2世代） | ¥39,800 |
| 8 | Nintendo Switch（有機ELモデル） | ¥37,980 |
| 9 | Sony α7R V ミラーレス一眼カメラ | ¥498,000 |
| 10 | Webカメラ（Logitech C920s HD Pro） | ¥9,800 |
| 11 | USB-Cハブ（Anker PowerExpand Elite） | ¥15,800 |
| 12 | 外付けSSD 2TB（Samsung T7 Touch） | ¥32,800 |

## 🛠️ トラブルシューティング

### Webアプリケーション関連

#### ブラウザでアクセスできない
```bash
# APIサーバーが起動しているか確認
curl http://localhost:8000/api/health

# プロセス確認
ps aux | grep uvicorn

# ポート使用状況確認
lsof -i :8000
```

#### API接続エラー（❌ API接続エラー）
```bash
# APIサーバーを再起動
./start_api.sh
```

#### MongoDB未接続エラー（⚠️ MongoDB未接続）
```bash
# MongoDB起動確認
brew services list | grep mongodb

# MongoDB起動
./start_mongodb.sh

# 接続テスト
.venv/bin/python test_mongodb_connection.py
```

### 従来の問題解決

#### MongoDB接続エラー
- MongoDBサーバーが起動していることを確認
- 接続文字列が正しいことを確認
- ファイアウォール設定を確認

#### JSONファイルエラー
- JSONファイルの形式が正しいことを確認
- ファイルのエンコーディングがUTF-8であることを確認

#### パーミッションエラー
- スクリプトに実行権限があることを確認
- ファイルディレクトリへの読み書き権限を確認

#### ポート競合エラー
```bash
# ポート8000を使用しているプロセスを確認
lsof -i :8000

# プロセスを停止（PIDを確認後）
kill -9 <PID>
```

## 🌟 主要機能

### Webアプリケーション
- ✅ **レスポンシブWebUI**: モバイル・デスクトップ対応
- ✅ **CRUD操作**: ユーザーデータの作成・読み取り・更新・削除
- ✅ **リアルタイム監視**: MongoDB接続状態・システム健康状態表示
- ✅ **インライン編集**: データの即座編集・削除機能  
- ✅ **モダンデザイン**: グラデーション・アニメーション効果

### バックエンドAPI
- ✅ **RESTful API**: FastAPI による高性能なWeb API
- ✅ **自動ドキュメント**: http://localhost:8000/docs でAPI仕様確認
- ✅ **CORS対応**: フロントエンドからの安全なアクセス
- ✅ **エラーハンドリング**: 適切なHTTPステータスコード
- ✅ **健康状態監視**: リアルタイムシステム状態確認

### データ管理
- ✅ **JSONファイル管理**: 複数ファイルの一括アップロード
- ✅ **MongoDB統合**: NoSQLデータベースによる柔軟なデータ管理  
- ✅ **データ検証**: 入力データの形式チェック
- ✅ **メタデータ追加**: ソースファイル名・アップロード日時の自動記録
- ✅ **バックアップ機能**: mongoexportによるデータエクスポート

## 🔄 MongoDB操作

### データベースとコレクションの確認
```bash
# MongoDBシェルに接続
mongosh

# データベース一覧
show dbs

# user_data_dbに切り替え
use user_data_db

# コレクション一覧
show collections

# usersコレクションのデータ表示
db.users.find().pretty()

# データ件数確認
db.users.countDocuments()
```

### データの削除
```bash
# 全データ削除
db.users.deleteMany({})

# 特定の条件でデータ削除
db.users.deleteMany({"source_file": "user_1.json"})
```

## 🔍 mongoshでのデータ取得方法

### 基本的なクエリ

#### 1. MongoDB Shellに接続
```bash
# ローカルMongoDBに接続
mongosh

# プロジェクトのデータベースに直接接続
mongosh mongodb://localhost:27017/user_data_db
```

#### 2. 基本的なデータ表示
```javascript
// プロジェクトデータベースに切り替え
use user_data_db

// 全データを表示
db.users.find()

// 見やすく整形して表示
db.users.find().pretty()

// 最初の1件のみ表示
db.users.findOne()

// 総件数を確認
db.users.countDocuments()
```

### 条件検索

#### 3. 基本的な検索クエリ
```javascript
// 特定のIDで検索
db.users.findOne({id: 1})

// 特定の商品を検索
db.users.find({product: "Laptop"}).pretty()

// ソースファイル別にデータを表示
db.users.find({source_file: "user_1.json"}).pretty()

// 価格範囲での検索
db.users.find({price: {$gte: 500, $lte: 2000}}).pretty()

// 複数の条件で検索
db.users.find({product: "Laptop", price: {$gt: 1000}}).pretty()
```

#### 4. 高度な検索
```javascript
// 正規表現を使用した検索
db.users.find({product: /^Lap/}).pretty()

// OR条件での検索
db.users.find({
  $or: [
    {price: {$lt: 600}},
    {product: "Smartphone"}
  ]
}).pretty()

// 存在チェック
db.users.find({uploaded_at: {$exists: true}}).pretty()

// 日付範囲での検索
db.users.find({
  uploaded_at: {
    $gte: new Date("2025-09-30"),
    $lt: new Date("2025-10-01")
  }
}).pretty()
```

### ソートと制限

#### 5. データの並び替えと制限
```javascript
// 価格の昇順でソート
db.users.find().sort({price: 1}).pretty()

// 価格の降順でソート
db.users.find().sort({price: -1}).pretty()

// 最新5件を表示
db.users.find().limit(5).pretty()

// 特定のフィールドのみ表示
db.users.find({}, {product: 1, price: 1, _id: 0}).pretty()

// ページング（2件スキップして3件表示）
db.users.find().skip(2).limit(3).pretty()
```

### 統計と集計

#### 6. 統計情報の取得
```javascript
// ユニークな商品名を取得
db.users.distinct("product")

// ソースファイル別の件数
db.users.aggregate([
  {$group: {
    _id: "$source_file",
    count: {$sum: 1}
  }}
])

// 価格統計（平均、最大、最小）
db.users.aggregate([
  {$group: {
    _id: null,
    avgPrice: {$avg: "$price"},
    maxPrice: {$max: "$price"},
    minPrice: {$min: "$price"},
    totalCount: {$sum: 1}
  }}
])

// 商品別の価格統計
db.users.aggregate([
  {$group: {
    _id: "$product",
    avgPrice: {$avg: "$price"},
    count: {$sum: 1}
  }},
  {$sort: {avgPrice: -1}}
])
```

### 便利な関数

#### 7. カスタム関数の定義（mongosh内で実行）
```javascript
// 統計情報を表示する関数
function showStats() {
    print("📈 統計情報:");
    const total = db.users.countDocuments();
    print(`総件数: ${total}`);
    
    const bySource = db.users.aggregate([
        {$group: {_id: "$source_file", count: {$sum: 1}}}
    ]).toArray();
    
    print("ソースファイル別:");
    bySource.forEach(item => print(`  ${item._id}: ${item.count}件`));
    
    const priceStats = db.users.aggregate([
        {$group: {
            _id: null,
            avg: {$avg: "$price"},
            max: {$max: "$price"},
            min: {$min: "$price"}
        }}
    ]).toArray()[0];
    
    if (priceStats) {
        print(`価格統計:`);
        print(`  平均: ￥${priceStats.avg?.toFixed(2) || 'N/A'}`);
        print(`  最高: ￥${priceStats.max || 'N/A'}`);
        print(`  最低: ￥${priceStats.min || 'N/A'}`);
    }
}

// 価格範囲で検索する関数
function findByPrice(min, max) {
    print(`💰 価格範囲 ￥${min} - ￥${max} の商品:`);
    db.users.find({
        price: {$gte: min, $lte: max}
    }).forEach(doc => {
        print(`${doc.product}: ￥${doc.price} (${doc.source_file})`);
    });
}

// 使用例
showStats()
findByPrice(500, 1500)
```

### データエクスポート

#### 8. コマンドラインからのエクスポート
```bash
# JSON形式でエクスポート
mongoexport --db=user_data_db --collection=users --out=export.json --pretty

# CSV形式でエクスポート
mongoexport --db=user_data_db --collection=users --type=csv --fields=id,product,price,source_file --out=export.csv

# 特定の条件でエクスポート
mongoexport --db=user_data_db --collection=users --query='{"price":{"$gt":1000}}' --out=expensive_items.json --pretty
```

### クイック接続スクリプト

#### 9. 便利な接続スクリプト
プロジェクト用の接続スクリプト `connect_mongodb.sh` を作成：

```bash
#!/bin/bash
echo "🔌 MongoDBに接続中..."
echo "📊 プロジェクト用データベース: user_data_db"
echo "📁 メインコレクション: users"
echo ""
echo "よく使うコマンド:"
echo "  show dbs                    - データベース一覧"
echo "  use user_data_db            - プロジェクトDBに切り替え"
echo "  show collections            - コレクション一覧"
echo "  db.users.find().pretty()    - データ表示"
echo "  db.users.countDocuments()   - 件数確認"
echo "  exit                        - 終了"
echo ""

# user_data_dbに直接接続
mongosh mongodb://localhost:27017/user_data_db
```

使用方法：
使用方法：
```bash
chmod +x connect_mongodb.sh
./connect_mongodb.sh
```

## 🚀 起動方法まとめ

### 🏃‍♂️ 最速起動（3ステップ）
```bash
# 1. セットアップ（初回のみ）
./setup.sh

# 2. MongoDB起動
./start_mongodb.sh

# 3. Webアプリ起動
./start_api.sh
```
**🌐 ブラウザで http://localhost:8000 にアクセス**

### 📝 初回セットアップ時
```bash
# 1. 環境構築
./setup.sh

# 2. MongoDB起動  
./start_mongodb.sh

# 3. サンプルデータ作成
.venv/bin/python main.py

# 4. Webアプリ起動
./start_api.sh
```

### 🔄 日常的な起動
```bash
# MongoDB確認・起動
./start_mongodb.sh

# Webアプリ起動
./start_api.sh
```

### 🛑 停止方法
```bash
# APIサーバー停止: Ctrl+C

# MongoDB停止
brew services stop mongodb-community
```

### 📋 アクセス先一覧
- **🏠 Web管理画面**: http://localhost:8000
- **📚 API ドキュメント**: http://localhost:8000/docs  
- **💚 健康状態確認**: http://localhost:8000/api/health
- **🗄️ MongoDB直接操作**: `mongosh mongodb://localhost:27017/user_data_db`

## 📄 ライセンス

MIT License

## MongoDB × Google ADK 技術検証ハンズオン

Google ADK の MongoDB 統合 (セッション永続化 / 長期記憶 / ベクトル・ハイブリッド検索) の検証ハンズオンは [adk_mongodb_handson/README.md](adk_mongodb_handson/README.md) を参照してください。
