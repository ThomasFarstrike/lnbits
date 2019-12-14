import lnurl
import os
import requests

from flask import Flask, jsonify, render_template, request

from . import bolt11
from .db import Database
from .helpers import encrypt
from .settings import DATABASE_PATH, LNBITS_PATH, WALLET


app = Flask(__name__)


def db_connect(db_path=DATABASE_PATH):
    import sqlite3

    con = sqlite3.connect(db_path)
    return con


@app.before_first_request
def init():
    with Database() as db:
        with open(os.path.join(LNBITS_PATH, "data", "schema.sql")) as schemafile:
            for stmt in schemafile.read().split("\n\n"):
                db.execute(stmt, [])


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/deletewallet")
def deletewallet():
    thewal = request.args.get("wal")

    with Database() as db:
        wallet_row = db.fetchone("SELECT * FROM wallets WHERE hash = ?", (thewal,))

        if not wallet_row:
            return render_template("index.html")

        db.execute("UPDATE wallets SET user = ? WHERE hash = ?", (f"del{wallet_row[4]}", wallet_row[0]))
        db.execute("UPDATE wallets SET adminkey = ? WHERE hash = ?", (f"del{wallet_row[5]}", wallet_row[0]))
        db.execute("UPDATE wallets SET inkey = ? WHERE hash = ?", (f"del{wallet_row[6]}", wallet_row[0]))

        user_wallets = db.fetchall("SELECT * FROM wallets WHERE user = ?", (wallet_row[4],))
        if user_wallets:
            return render_template("deletewallet.html", theid=user_wallets[0][4], thewal=user_wallets[0][0])

    return render_template("index.html")


@app.route("/lnurlwallet")
def lnurlwallet():
    withdraw_res = lnurl.handle(request.args.get("lightning"))
    invoice = WALLET.create_invoice(withdraw_res.max_sats).json()
    payment_hash = invoice["payment_hash"]

    rrr = requests.get(
        withdraw_res.callback.base,
        params={**withdraw_res.callback.query_params, **{"k1": withdraw_res.k1, "pr": invoice["pay_req"]}},
    )
    dataaa = rrr.json()

    if dataaa["status"] != "OK":
        """TODO: show some kind of error?"""
        return render_template("index.html")

    data = ""
    while data == "":
        r = WALLET.get_invoice_status(payment_hash)
        data = r.json()

    with Database() as db:
        adminkey = encrypt(payment_hash)[0:20]
        inkey = encrypt(adminkey)[0:20]
        thewal = encrypt(inkey)[0:20]
        theid = encrypt(thewal)[0:20]
        thenme = "Bitcoin LN Wallet"

        db.execute("INSERT INTO accounts (userhash) VALUES (?)", (theid,))

        adminkey = encrypt(theid)
        inkey = encrypt(adminkey)

        db.execute(
            "INSERT INTO wallets (hash, name, user, adminkey, inkey) VALUES (?, ?, ?, ?, ?)",
            (thewal, thenme, theid, adminkey, inkey),
        )

    return render_template(
        "lnurlwallet.html",
        len=len("1"),
        walnme=thenme,
        walbal=withdraw_res.max_sats,
        theid=theid,
        thewal=thewal,
        adminkey=adminkey,
        inkey=inkey,
    )


@app.route("/wallet")
def wallet():
    theid = request.args.get("usr")
    thewal = request.args.get("wal")
    thenme = request.args.get("nme")

    if not thewal:
        return render_template("index.html")

    with Database() as db:
        user_exists = db.fetchone("SELECT * FROM accounts WHERE userhash = ?", (theid,))

        if not user_exists:
            # user does not exist: create an account
            # --------------------------------------

            db.execute("INSERT INTO accounts (userhash) VALUES (?)", (theid,))

        # user exists
        # -----------

        user_wallets = db.fetchall("SELECT * FROM wallets WHERE user = ?", (theid,))

        if user_wallets:

            # user has wallets
            # ----------------

            wallet_row = db.fetchone(
                """
              SELECT
                (SELECT balance/1000 FROM balances WHERE wallet = wallets.hash),
                name,
                adminkey,
                inkey
              FROM wallets
              WHERE user = ? AND hash = ?
            """,
                (theid, thewal,),
            )

            transactions = []

            return render_template(
                "wallet.html",
                thearr=user_wallets,
                len=len(user_wallets),
                walnme=wallet_row[1],
                user=theid,
                walbal=wallet_row[0],
                theid=theid,
                thewal=thewal,
                transactions=transactions,
                adminkey=wallet_row[2],
                inkey=wallet_row[3],
            )

        # user has no wallets
        # -------------------

        adminkey = encrypt(theid)
        inkey = encrypt(adminkey)

        db.execute(
            "INSERT INTO wallets (hash, name, user, adminkey, inkey) VALUES (?, ?, ?, ?, ?)",
            (thewal, thenme, theid, adminkey, inkey),
        )

        return render_template(
            "wallet.html",
            len=1,
            walnme=thenme,
            walbal=0,
            theid=theid,
            thewal=thewal,
            adminkey=adminkey,
            inkey=inkey,
            transactions=[],
        )


@app.route("/v1/invoices", methods=["GET", "POST"])
def api_invoices():
    if request.headers["Content-Type"] != "application/json":
        return jsonify({"ERROR": "MUST BE JSON"}), 400

    postedjson = request.json

    if "value" not in postedjson:
        return jsonify({"ERROR": "NO VALUE"}), 400

    if not postedjson["value"].isdigit():
        return jsonify({"ERROR": "VALUE MUST BE A NUMBER"}), 400

    if int(postedjson["value"]) < 0:
        return jsonify({"ERROR": "AMOUNTLESS INVOICES NOT SUPPORTED"}), 400

    if "memo" not in postedjson:
        return jsonify({"ERROR": "NO MEMO"}), 400

    with Database() as db:
        wallet_row = db.fetchone(
            "SELECT hash FROM wallets WHERE inkey = ? OR adminkey = ?",
            (request.headers["Grpc-Metadata-macaroon"], request.headers["Grpc-Metadata-macaroon"],),
        )

        if not wallet_row:
            return jsonify({"ERROR": "NO KEY"}), 200

        r = WALLET.create_invoice(postedjson["value"], postedjson["memo"])
        data = r.json()

        pay_req = data["pay_req"]
        payment_hash = data["payment_hash"]

        db.execute(
            "INSERT INTO apipayments (payhash, amount, wallet, pending, memo) VALUES (?, ?, ?, true, ?)",
            (payment_hash, int(postedjson["value"]) * 1000, wallet_row[0], postedjson["memo"],),
        )

    return jsonify({"pay_req": pay_req, "payment_hash": payment_hash}), 200


@app.route("/v1/channels/transactions", methods=["GET", "POST"])
def api_transactions():
    if request.headers["Content-Type"] != "application/json":
        return jsonify({"ERROR": "MUST BE JSON"}), 200

    data = request.json

    if "payment_request" not in data:
        return jsonify({"ERROR": "NO PAY REQ"}), 200

    with Database() as db:
        wallet_row = db.fetchone(
            "SELECT hash FROM wallets WHERE adminkey = ?", (request.headers["Grpc-Metadata-macaroon"],)
        )

        if not wallet_row:
            return jsonify({"ERROR": "BAD AUTH"}), 200

        # TODO: check this unused code
        # move sats calculation to a helper
        # ---------------------------------
        """
        s = postedjson["payment_request"]
        result = re.search("lnbc(.*)1p", s)
        tempp = result.group(1)

        alpha = ""
        num = ""

        for i in range(len(tempp)):
            if tempp[i].isdigit():
                num = num + tempp[i]
            else:
                alpha += tempp[i]
        sats = ""
        if alpha == "n":
            sats = int(num) / 10
        elif alpha == "u":
            sats = int(num) * 100
        elif alpha == "m":
            sats = int(num) * 100000
        """
        # ---------------------------------

        # decode the invoice
        invoice = bolt11.decode(data["payment_request"])
        if invoice.amount_msat == 0:
            return jsonify({"ERROR": "AMOUNTLESS INVOICES NOT SUPPORTED"}), 400

        # insert the payment
        db.execute(
            "INSERT INTO apipayments (payhash, amount, fee, wallet, pending, memo) VALUES (?, ?, ?, ?, true, ?)",
            (
                invoice.payment_hash,
                -int(invoice.amount_msat),
                -int(invoice.amount_msat * 0.01),
                wallet_row[0],
                invoice.description,
            ),
        )

        # check balance
        balance = db.fetchone("SELECT balance/1000 FROM balances WHERE wallet = ?", (wallet_row[0],))[0]
        if balance < 0:
            return jsonify({"ERROR": "INSUFFICIENT BALANCE"}), 403

        # actually send the payment
        r = WALLET.pay_invoice(data["payment_request"])

        if not r.ok:
            return jsonify({"ERROR": "UNEXPECTED PAYMENT ERROR"}), 500

        data = r.json()
        if r.ok and "error" in data:
            # payment didn't went through, delete it here
            # (these guarantees specific to lntxbot)
            db.execute("DELETE FROM apipayments WHERE payhash = ?", (invoice.payment_hash,))
            return jsonify({"PAID": "FALSE"}), 200

        # payment went through, not pending anymore, save actual fees
        db.execute(
            "UPDATE apipayments SET pending = false, fee = ? WHERE payhash = ?",
            (data["fee_msat"], invoice.payment_hash,),
        )

    return jsonify({"PAID": "TRUE"}), 200


@app.route("/v1/invoice/<payhash>", methods=["GET"])
def api_checkinvoice(payhash):
    if request.headers["Content-Type"] != "application/json":
        return jsonify({"ERROR": "MUST BE JSON"}), 200

    with Database() as db:
        payment_row = db.fetchone(
            """
          SELECT pending FROM apipayments
          INNER JOIN wallets AS w ON apipayments.wallet = w.hash
          WHERE payhash = ?
            AND (w.adminkey = ? OR w.inkey = ?)
        """,
            (payhash, request.headers["Grpc-Metadata-macaroon"], request.headers["Grpc-Metadata-macaroon"]),
        )

        if not payment_row:
            return jsonify({"ERROR": "NO INVOICE"}), 404

        if not payment_row[0]:  # pending
            return jsonify({"PAID": "TRUE"}), 200

        r = WALLET.get_invoice_status(payhash)

        if not r.ok:
            return jsonify({"PAID": "FALSE"}), 400

        data = r.json()
        if "preimage" not in data or not data["preimage"]:
            return jsonify({"PAID": "FALSE"}), 400

        db.execute("UPDATE apipayments SET pending = false WHERE payhash = ?", (payhash,))
        return jsonify({"PAID": "TRUE"}), 200