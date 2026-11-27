import os
import time
import uuid
from datetime import datetime, timedelta, timezone

from azure.core import MatchConditions
from azure.core.exceptions import ResourceModifiedError, ResourceNotFoundError
from azure.data.tables import TableServiceClient, UpdateMode
from flask import Flask, abort, redirect, render_template, request, url_for

app = Flask(__name__)

# 環境変数から接続文字列を読み込む（コードには書かない）
CONNECTION_STRING = os.environ["AZURE_STORAGE_CONNECTION_STRING"]

# 日本時間
JST = timezone(timedelta(hours=9))

# 教科の選択肢
SUBJECTS = ["数学", "英語", "国語", "理科", "社会", "その他"]

# Table Storageに接続し、テーブルがなければ作る
#   posts   ：質問を入れるテーブル
#   answers ：回答を入れるテーブル
service = TableServiceClient.from_connection_string(CONNECTION_STRING)
posts_table = service.create_table_if_not_exists("posts")
answers_table = service.create_table_if_not_exists("answers")

# Table Storageのデータは「PartitionKey」と「RowKey」の組で1件を特定し、
# RowKeyの小さい順（文字の並び順）に返ってくる。
MAX_MS = 10**13


def new_post_key():
    """質問のRowKey。新しい質問ほど小さい値になり、一覧で新しい順に並ぶ"""
    now_ms = int(time.time() * 1000)
    return f"{MAX_MS - now_ms:013d}-{uuid.uuid4().hex[:6]}"


def new_answer_key():
    """回答のRowKey。新しい回答ほど大きい値になり、古い順に並ぶ"""
    now_ms = int(time.time() * 1000)
    return f"{now_ms:013d}-{uuid.uuid4().hex[:6]}"


def fmt(dt, pattern):
    """保存してある日時を日本時間の文字列にする"""
    return dt.astimezone(JST).strftime(pattern)


def to_post(entity):
    """テーブルの1行を、画面で使いやすい辞書にする"""
    return {
        "id": entity["RowKey"],
        "subject": entity["subject"],
        "name": entity["name"],
        "title": entity["title"],
        "body": entity["body"],
        "solved": entity["solved"],
        "wakaru": entity["wakaru"],
        "answer_count": entity["answer_count"],
        "created_short": fmt(entity["created_at"], "%m/%d %H:%M"),
        "created_full": fmt(entity["created_at"], "%Y/%m/%d %H:%M"),
    }


def get_post_entity_or_404(post_id):
    try:
        return posts_table.get_entity(partition_key="post", row_key=post_id)
    except ResourceNotFoundError:
        abort(404)


def add_to_counter(post_id, field):
    """数（わかる・回答数）を1増やす。

    Table Storageには「1足す」命令がないので、
    ①今の値を読む → ②1足して書き戻す、の2段階で行う。
    読んだ後に誰かが書き換えていたら書き戻しは失敗するので、やり直す。
    """
    for _ in range(5):
        entity = get_post_entity_or_404(post_id)
        try:
            posts_table.update_entity(
                {
                    "PartitionKey": "post",
                    "RowKey": post_id,
                    field: entity[field] + 1,
                },
                mode=UpdateMode.MERGE,
                etag=entity.metadata["etag"],
                match_condition=MatchConditions.IfNotModified,
            )
            return
        except ResourceModifiedError:
            continue


@app.route("/")
def index():
    """質問一覧。?subject=数学 で教科絞り込み、?unsolved=1 で未解決のみ"""
    subject = request.args.get("subject")
    unsolved = request.args.get("unsolved") == "1"

    # 絞り込み条件を組み立てる（値は @ の名前を付けて別に渡す）
    conditions = ["PartitionKey eq 'post'"]
    params = {}
    if subject in SUBJECTS:
        conditions.append("subject eq @subject")
        params["subject"] = subject
    if unsolved:
        conditions.append("solved eq false")

    entities = posts_table.query_entities(" and ".join(conditions), parameters=params)
    posts = [to_post(e) for e in entities]

    return render_template(
        "index.html",
        posts=posts,
        subjects=SUBJECTS,
        current=subject,
        unsolved=unsolved,
    )


@app.route("/new", methods=["GET", "POST"])
def new_post():
    """質問フォーム（GET）と、質問の保存（POST）"""
    if request.method == "POST":
        subject = request.form.get("subject", "")
        name = request.form.get("name", "").strip()
        title = request.form.get("title", "").strip()
        body = request.form.get("body", "").strip()
        # 入力チェック：空欄や、選択肢にない教科は受け付けない
        if subject not in SUBJECTS or not (name and title and body):
            return render_template(
                "new.html", subjects=SUBJECTS, error="すべて入力してください"
            ), 400
        posts_table.create_entity(
            {
                "PartitionKey": "post",
                "RowKey": new_post_key(),
                "subject": subject,
                "name": name[:30],
                "title": title[:60],
                "body": body,
                "solved": False,
                "wakaru": 0,
                "answer_count": 0,
                "created_at": datetime.now(JST),
            }
        )
        return redirect(url_for("index"))
    return render_template("new.html", subjects=SUBJECTS, error=None)


def render_detail(post_id, error=None, status=200):
    post = to_post(get_post_entity_or_404(post_id))
    # この質問への回答だけを取り出す（PartitionKeyに質問のIDを入れてある）
    answers = [
        {
            "name": e["name"],
            "body": e["body"],
            "created_short": fmt(e["created_at"], "%m/%d %H:%M"),
        }
        for e in answers_table.query_entities(
            "PartitionKey eq @pid", parameters={"pid": post_id}
        )
    ]
    return render_template("detail.html", post=post, answers=answers, error=error), status


@app.route("/post/<post_id>", methods=["GET", "POST"])
def detail(post_id):
    """質問の詳細表示（GET）と、回答の保存（POST）"""
    if request.method == "POST":
        get_post_entity_or_404(post_id)
        name = request.form.get("name", "").strip()
        body = request.form.get("body", "").strip()
        if not (name and body):
            return render_detail(post_id, "ニックネームと回答を入力してください", 400)
        answers_table.create_entity(
            {
                "PartitionKey": post_id,
                "RowKey": new_answer_key(),
                "name": name[:30],
                "body": body,
                "created_at": datetime.now(JST),
            }
        )
        add_to_counter(post_id, "answer_count")
        return redirect(url_for("detail", post_id=post_id))
    return render_detail(post_id)


@app.route("/post/<post_id>/wakaru", methods=["POST"])
def wakaru(post_id):
    """「わかる！」を1増やす"""
    add_to_counter(post_id, "wakaru")
    return redirect(url_for("detail", post_id=post_id))


@app.route("/post/<post_id>/solved", methods=["POST"])
def toggle_solved(post_id):
    """「解決した！」：解決済み／未解決を切り替える"""
    entity = get_post_entity_or_404(post_id)
    posts_table.update_entity(
        {"PartitionKey": "post", "RowKey": post_id, "solved": not entity["solved"]},
        mode=UpdateMode.MERGE,
    )
    return redirect(url_for("detail", post_id=post_id))


if __name__ == "__main__":
    app.run(debug=True)