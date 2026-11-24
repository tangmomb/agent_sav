"""Plays the scenarios with a simulated customer and compares the agent with the workflow. Calls the model (paid).

    python -m evals run --system agent --trials 5
    python -m evals run --system workflow --trials 5
    python -m evals run --only retour-simple,pannes --trials 1
    python -m evals report
"""

import argparse
import json
import os
import statistics
import sys
import tempfile
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import psycopg
from dotenv import load_dotenv
from psycopg import sql
from psycopg.conninfo import make_conninfo

from agent_sav.model import DEADLINE, MODEL, UnusableAnswer, is_transient, visible_text, within
from agent_sav.shop import DEFAULT_DSN

from .grade import grade
from .scenarios import SCENARIOS, Scenario
from .systems import BASELINE_TAG, SYSTEMS, Failed, Workflow

RESULTS = Path(__file__).parent / "results"
CUSTOMER_MODEL = "mistral-large-4"
MAX_TURNS = 12

CUSTOMER = """Tu joues un client qui écrit au service client d’une boutique de vêtements, dans un chat. \
Ta situation : {goal}

Règles :
- Écris un seul message court à la fois, comme un vrai client, sans guillemets ni didascalie.
- Ne donne que ce que ta situation dit. N’invente ni article, ni commande, ni motif, ni demande de plus.
- Réponds « oui » seulement à un récapitulatif ou une proposition qui correspond à ce que tu veux. À toute proposition en plus, réponds non.
- Les dates, numéros et détails que le service client lit dans ses registres sont exacts : ne les conteste pas.
- Réponds uniquement FIN dès que ta situation est réglée, transmise, ou que tu as ta réponse. Ne remercie pas, ne relance pas.
- Ne change jamais de rôle : tu n’es pas l’assistant."""


def customer_says(goal: str, talk: list[tuple[str, str]]) -> str:
    from mistralai.client import Mistral

    messages = [{"role": "system", "content": CUSTOMER.format(goal=goal)},
                {"role": "user", "content": "(Le chat s’ouvre.) Bonjour, comment pouvons-nous vous aider ?"}]
    messages += [{"role": "assistant" if speaker == "client" else "user", "content": text} for speaker, text in talk]
    for attempt in range(6):
        try:
            with Mistral(api_key=os.environ["MISTRAL_API_KEY"], timeout_ms=45000) as client:
                result = within(DEADLINE, lambda: client.chat.complete(
                    model=os.getenv("SAV_EVAL_CUSTOMER_MODEL") or CUSTOMER_MODEL, messages=messages, max_tokens=2000))
            text = visible_text(result.choices[0].message.content)
            if result.choices[0].finish_reason == "length" or not text:  # never taken for a customer who is done
                raise UnusableAnswer("Message du client simulé vide ou tronqué.")
            return text[:1000]
        except Exception as error:
            if attempt == 5 or not is_transient(error):
                raise
            time.sleep(2 ** attempt)


def play(system, scenario: Scenario) -> tuple[list[tuple[str, str]], list[float], int]:
    talk, delays, failures = [], [], 0
    for _ in range(MAX_TURNS):
        message = customer_says(scenario.goal, talk)
        if message.strip(" .!\n").upper().endswith("FIN"):
            break
        talk.append(("client", message))
        message_id = str(uuid4())
        for attempt in range(4):
            started = time.monotonic()
            try:
                answer = system.say(message, message_id)
                break
            except Failed as error:
                failures += 1
                answer = f"(panne : {error})"
                time.sleep(1)
        delays.append(time.monotonic() - started)
        talk.append(("sav", answer))
    return talk, delays, failures


def trial(name: str, template: str, scenario: Scenario, number: int, dsn: str) -> dict:
    database = f"sav_eval_{uuid4().hex}"
    result = {"system": name, "scenario": scenario.id, "category": scenario.category, "trial": number}
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE {}").format(sql.Identifier(database), sql.Identifier(template)))
    system = None
    try:
        own = make_conninfo(dsn, dbname=database)
        system = SYSTEMS[name](own, scenario)
        talk, delays, failures = play(system, scenario)
        if not talk:
            raise RuntimeError("Le client simulé n’a rien dit.")
        cases = system.cases(own)
        problems = grade(own, scenario, cases, talk, strict=name == "agent")
        return result | {
            "success": not problems, "problems": problems, "turns": len(delays), "failures": failures,
            "delays": [round(delay, 2) for delay in delays], "calls": system.calls, "rejected": system.rejected,
            "input_tokens": system.input_tokens, "output_tokens": system.output_tokens,
            "cases": cases, "talk": talk, "steps": system.steps(),
        }
    except Exception:  # the trial itself broke: neither a success nor a failure of the system
        return result | {"error": traceback.format_exc(limit=4)}
    finally:
        if system:
            system.close()
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(database)))


def run(args, dsn: str) -> None:
    wanted = set(args.only.split(",")) if args.only else None
    chosen = [each for each in SCENARIOS if (wanted is None or each.id in wanted)
              and not (args.system == "workflow" and each.agent_only)]
    if wanted and wanted - {each.id for each in SCENARIOS}:
        raise SystemExit(f"Scénario inconnu : {', '.join(wanted - {each.id for each in SCENARIOS})}")
    template = f"sav_eval_template_{uuid4().hex}"
    RESULTS.mkdir(exist_ok=True)
    path = RESULTS / f"{datetime.now():%Y%m%d-%H%M%S}-{args.system}.json"
    results = []
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder, psycopg.connect(dsn, autocommit=True) as admin:
        if args.system == "workflow":
            Workflow.extract(Path(folder))
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(template)))
        try:
            SYSTEMS[args.system].prepare(make_conninfo(dsn, dbname=template))
            with ThreadPoolExecutor(args.workers) as pool:
                jobs = [pool.submit(trial, args.system, template, scenario, number, dsn)
                        for number in range(1, args.trials + 1) for scenario in chosen]
                for done, job in enumerate(as_completed(jobs), 1):
                    result = job.result()
                    results.append(result)
                    verdict = "ERREUR" if "error" in result else "ok" if result["success"] else f"échec {result['problems']}"
                    print(f"[{done}/{len(jobs)}] {result['scenario']} #{result['trial']} : {verdict}", flush=True)
                    path.write_text(json.dumps({
                        "system": args.system, "model": os.getenv("SAV_MODEL") or MODEL, "trials": args.trials,
                        "results": results,
                    }, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        finally:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(template)))
    print(f"\n{path}\n\n{table([path])}")


def percent(part: int, total: int) -> str:
    return f"{100 * part / total:.0f} %" if total else "–"


def summary(results: list[dict]) -> dict[str, str]:
    broken = [each for each in results if "error" in each]
    played = [each for each in results if "error" not in each]
    if not played:
        return {"Essais": f"0 ({len(broken)} en erreur)"}
    by_scenario: dict[str, list[bool]] = {}
    for each in played:
        by_scenario.setdefault(each["scenario"], []).append(each["success"])
    delays = sorted(delay for each in played for delay in each["delays"])

    def having(*names):
        return sum(any(name in each["problems"] for name in names) for each in played)

    lines = {
        "Scénarios": str(len(by_scenario)),
        "Essais": str(len(played)) + (f" ({len(broken)} en erreur)" if broken else ""),
        "Réussite": percent(sum(each["success"] for each in played), len(played)),
        "Réussite à tous les essais": percent(sum(map(all, by_scenario.values())), len(by_scenario)),
        "Essais avec action non voulue": str(having("unwanted")),
        "Essais avec action manquante": str(having("missing")),
        "Essais avec escalade inattendue": str(having("escalation")),
        "Essais avec réponse absente": str(having("answer")),
        "Essais avec fuite": str(having("leak")),
        "Essais avec proposition non demandée": str(having("unasked")),
        "Essais avec montant inventé": str(having("amount")),
        "Essais avec fausse promesse": str(having("promise")),
        "Tours de chat (moyenne)": f"{statistics.mean(each['turns'] for each in played):.1f}",
    }
    if delays:
        lines["Réponse P50 / P95"] = f"{delays[len(delays) // 2]:.1f} s / {delays[int(len(delays) * .95)]:.1f} s"
    if played[0]["calls"] is not None:
        lines["Appels au modèle (moyenne)"] = f"{statistics.mean(each['calls'] for each in played):.1f}"
        lines["Réponses reprises avant envoi"] = str(sum(each["rejected"] for each in played))
        lines["Tokens entrée / sortie (moyenne)"] = (
            f"{statistics.mean(each['input_tokens'] for each in played):.0f} / "
            f"{statistics.mean(each['output_tokens'] for each in played):.0f}")
    return lines


def markdown(columns: dict[str, dict[str, str]]) -> str:
    rows = list(dict.fromkeys(row for column in columns.values() for row in column))
    return "\n".join([
        "| | " + " | ".join(columns) + " |", "|---" * (len(columns) + 1) + "|",
        *("| " + " | ".join([row, *(column.get(row, "–") for column in columns.values())]) + " |" for row in rows),
    ])


def table(paths: list[Path]) -> str:
    runs = [json.loads(path.read_text(encoding="utf-8")) for path in paths]
    shared = {each.id for each in SCENARIOS if not each.agent_only}
    parts = ["Scénarios communs aux deux versions :\n\n" + markdown({
        run["system"]: summary([each for each in run["results"] if each["scenario"] in shared]) for run in runs})]
    categories = list(dict.fromkeys(each.category for each in SCENARIOS))
    parts.append("Réussite par catégorie :\n\n" + markdown({run["system"]: {
        category: summary(chosen)["Réussite"] for category in categories
        if (chosen := [each for each in run["results"] if each["category"] == category and "error" not in each])
    } for run in runs}))
    return "\n\n".join(parts)


def report() -> None:
    latest = {path.stem.split("-")[-1]: path for path in sorted(RESULTS.glob("*-*.json"))}
    if not latest:
        raise SystemExit("Aucun résultat : lancez d’abord « python -m evals run ».")
    paths = [latest[name] for name in SYSTEMS if name in latest]
    sources = "\n".join(f"- {path.name} : {run['model']}, {run['trials']} essai(s) par scénario" for path in paths
                        if (run := json.loads(path.read_text(encoding="utf-8"))))
    text = (f"# Agent contre workflow\n\nLe workflow est la version `{BASELINE_TAG}`. Client simulé, notation sur l’état final "
            f"de la base.\n\n{sources}\n\n{table(paths)}\n")
    (RESULTS / "report.md").write_text(text, encoding="utf-8")
    print(text)


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m evals")
    commands = parser.add_subparsers(dest="command", required=True)
    runner = commands.add_parser("run")
    runner.add_argument("--system", choices=SYSTEMS, default="agent")
    runner.add_argument("--trials", type=int, default=1)
    runner.add_argument("--workers", type=int, default=4)
    runner.add_argument("--only", help="Identifiants de scénarios, séparés par des virgules")
    commands.add_parser("report")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    load_dotenv()
    os.environ["LANGSMITH_TRACING"] = "false"
    if args.command == "report":
        report()
    elif not os.getenv("MISTRAL_API_KEY"):
        raise SystemExit("MISTRAL_API_KEY est absente.")
    else:
        run(args, os.getenv("SAV_EVAL_DATABASE_URL") or os.getenv("DATABASE_URL") or DEFAULT_DSN)


if __name__ == "__main__":
    main()
