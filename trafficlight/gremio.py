"""Grêmio mode: the last result and next match from ESPN."""
from __future__ import annotations
import logging
import requests
from typing import Dict, Optional
from .config import API_TIMEOUT
from .net import _poll

GREMIO_POLL_INTERVAL = 3600.0
GREMIO_ESPN_TEAM_ID = "6273"

# Every competition Grêmio might be playing in at once: domestic league + the CONMEBOL cups.
GREMIO_ESPN_LEAGUES = ["bra.1", "bra.copa_do_brazil", "conmebol.libertadores", "conmebol.sudamericana", "conmebol.recopa"]

def _gremio_schedule_url(league: str) -> str:
    return f"https://site.api.espn.com/apis/site/v2/sports/soccer/{league}/teams/{GREMIO_ESPN_TEAM_ID}/schedule"

def handle_gremio_mode(controller, elapsed: float) -> Optional[float]:
    result = (controller.state.gremio_status or {}).get('result')
    c = controller.state.current_color
    if result == 'win':
        controller.set_light_state('green')
    elif result == 'draw':
        controller.set_light_state('yellow')
    elif result == 'loss':
        controller.set_light_state('red')
    else:
        # No data: slow red blink.
        controller.set_light_state('red' if c != 'red' else 'off')
        return 0.5
    return None

def _espn_competitor_score(competitor: Dict) -> int:
    s = competitor.get('score')
    if isinstance(s, dict):
        return int(float(s.get('displayValue') or 0))
    return int(float(s or 0))

def _fetch_gremio_last_result() -> Dict:
    out: Dict = {}

    # Last completed match, across every tracked competition: newest completed
    # event per league (sorted here rather than trusting ESPN's order), then the
    # newest of those.
    best_ev, best_competitors = None, None
    for league in GREMIO_ESPN_LEAGUES:
        try:
            r = requests.get(_gremio_schedule_url(league), timeout=API_TIMEOUT)
            r.raise_for_status()
            events = sorted(r.json().get('events') or [], key=lambda e: e.get('date') or '', reverse=True)
            for ev in events:
                comp = (ev.get('competitions') or [{}])[0]
                if not (comp.get('status') or {}).get('type', {}).get('completed'):
                    continue
                if best_ev is None or (ev.get('date') or '') > (best_ev.get('date') or ''):
                    best_ev, best_competitors = ev, comp.get('competitors') or []
                break
        except Exception as e:
            logging.debug(f"Grêmio ESPN schedule fetch failed ({league}): {e}")

    if best_ev is not None:
        gremio_c = next((c for c in best_competitors if 'grêmio' in (c.get('team', {}).get('displayName') or '').lower()), None)
        opp_c = next((c for c in best_competitors if c is not gremio_c), None)
        if gremio_c and opp_c:
            gs = _espn_competitor_score(gremio_c)
            os_ = _espn_competitor_score(opp_c)
            # ESPN's winner flag also covers penalty shootouts, where the goals are level.
            if gremio_c.get('winner'):
                match_result = 'win'
            elif opp_c.get('winner') or gs < os_:
                match_result = 'loss'
            else:
                match_result = 'draw'
            out.update({
                'result': match_result,
                'score': f"{gs}-{os_}",
                'opponent': (opp_c.get('team') or {}).get('displayName', '?'),
                'home_away': 'home' if gremio_c.get('homeAway') == 'home' else 'away',
                'date': best_ev.get('date') or '',
                'competition': (best_ev.get('league') or {}).get('name'),
            })

    # Next upcoming match, across every tracked competition: each league's team
    # fixtures (the scoreboard's date-range query answers 400).
    best_next, best_next_competitors = None, None
    for league in GREMIO_ESPN_LEAGUES:
        try:
            r = requests.get(_gremio_schedule_url(league), params={'fixture': 'true'}, timeout=API_TIMEOUT)
            r.raise_for_status()
            events = sorted(r.json().get('events') or [], key=lambda e: e.get('date') or '')
            for ev in events:
                comp = (ev.get('competitions') or [{}])[0]
                if (comp.get('status') or {}).get('type', {}).get('state') != 'pre':
                    continue  # already played, or under way
                if best_next is None or (ev.get('date') or '') < (best_next.get('date') or ''):
                    best_next, best_next_competitors = ev, comp.get('competitors') or []
                break
        except Exception as e:
            logging.debug(f"Grêmio ESPN next match fetch failed ({league}): {e}")

    if best_next is not None:
        gremio_c = next((c for c in best_next_competitors if 'grêmio' in (c.get('team', {}).get('displayName') or '').lower()), None)
        opp_c = next((c for c in best_next_competitors if c is not gremio_c), None) if gremio_c else None
        out['next_match_date'] = best_next.get('date', '')
        out['next_match_opponent'] = (opp_c.get('team') or {}).get('displayName', '?') if opp_c else '?'
        out['next_match_home_away'] = 'home' if (gremio_c or {}).get('homeAway') == 'home' else 'away'

    # Without a last result the mode has nothing to show; count it as a failed fetch.
    return out if 'result' in out else {}

def gremio_monitor(controller):
    _poll(controller, 'Grêmio', _fetch_gremio_last_result, GREMIO_POLL_INTERVAL,
          lambda v: setattr(controller.state, 'gremio_status', v))
