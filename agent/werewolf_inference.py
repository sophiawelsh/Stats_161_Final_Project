"""Transparent, untrained Werewolf evidence model. Adjust field mappings to server schema."""
import math
import re
from collections import defaultdict

ROLES = ('werewolf', 'villager', 'seer')
WEIGHTS = {'changed_claim': 0.65, 'voted_known_wolf': -0.8,
           'voted_known_nonwolf': 0.2, 'defended_known_wolf': 0.7}

def seat(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    match = re.fullmatch(r'(?:player\s*)?(\d+)', str(value), re.I)
    return int(match[1]) if match else None


def role(value):
    value = str(value).strip().lower()
    return value if value in ROLES else None


def rows(value):
    return [x for x in value if isinstance(x, dict)] if isinstance(value, list) else []


class WerewolfInference:
    def __init__(self):
        self.claims = defaultdict(set)
        self.accusations = defaultdict(set)
        self.defenses = defaultdict(set)
        self.votes = set()
        self.known = {}
        self.nonwolves = set()
        self.seen = set()
        self.players = set()
        self.public_known = {}
        self.investigation_history = {}
        self.public_claims = defaultdict(set)
        self.public_accusations = defaultdict(set)
        self.public_defenses = defaultdict(set)
        self.dead = set()

    def update(self, state, context):
        raw = state.raw
        game = raw.get('game_state') or {}
        obs = getattr(state, 'observation', None)
        if not isinstance(obs, dict):
            obs = raw.get('observation') or {}
        if not isinstance(obs, dict):
            obs = {}
        if not isinstance(game, dict):
            game = {}
        me = seat(context.seat_position)
        self.players.update(p for x in rows(raw.get('players'))
                            if (p := seat(x.get('position'))) is not None)
        if me is not None:
            self.players.add(me)
            if role(obs.get('role')):
                self.known[me] = role(obs['role'])
        # Only explicitly revealed deaths and own private observations are trusted.
        for x in rows(game.get('deaths')):
            p = seat(x.get('position', x.get('player', x.get('seat'))))
            r = role(x.get('true_role', x.get('role')))
            if p is not None:
                self.dead.add(p)
                self.players.add(p)
                if r:
                    self.known[p] = r
                    self.public_known[p] = r
        if self.known.get(me) == 'seer':
            for x in rows(obs.get('investigations')):
                p = seat(x.get('position', x.get('target')))
                r = role(x.get('role'))
                if p is None:
                    continue
                if r or x.get('is_werewolf') is True:
                    r = r or 'werewolf'
                    self.known[p] = r
                    self.investigation_history[p] = r
                    if r == 'werewolf':
                        self.nonwolves.discard(p)
                    else:
                        self.nonwolves.add(p)
                elif x.get('is_werewolf') is False:
                    self.nonwolves.add(p)
                    self.investigation_history[p] = 'nonwolf'
        if self.known.get(me) == 'werewolf':
            for x in obs.get('werewolf_teammates', []) or []:
                p = seat(x)
                if p is not None:
                    self.known[p] = 'werewolf'
        # Expected vote format: [{day, voter, target}, ...]. Ignore unknown formats.
        for x in rows(game.get('votes', game.get('past_votes'))):
            v, t = seat(x.get('voter')), seat(x.get('target'))
            if v in self.players and t in self.players and v != t and x.get('day') is not None:
                self.votes.add((str(x['day']), v, t))
        for msg in state.new_messages:
            if msg.index in self.seen:
                continue
            self.seen.add(msg.index)
            p = seat(msg.sender)
            if p not in self.players:
                continue
            public = getattr(msg, 'recipients', None) == [] or getattr(msg, 'recipients', None) == ()
            text = str(msg.content).lower().replace('’', "'")
            # Conservative patterns; claims are NOT confirmed facts.
            for r in re.findall(r"\b(?:i am|i'm) (?:a |the )?(werewolf|villager|seer)\b", text):
                self.claims[p].add(r)
                if public:
                    self.public_claims[p].add(r)
            for pattern, store in [(r'\bi suspect player\s*(\d+)\b', self.accusations),
                                   (r'\bi trust player\s*(\d+)\b', self.defenses)]:
                for target in re.findall(pattern, text):
                    if int(target) in self.players and int(target) != p:
                        store[p].add(int(target))
                        if public:
                            (self.public_accusations if store is self.accusations else self.public_defenses)[p].add(int(target))
        analysis = self.report(me)
        analysis['your_role'] = self.known.get(me)
        analysis['public_perspective'] = self.report(me, public=True)['players']
        if self.known.get(me) == 'seer':
            analysis['seer_analysis'] = {
                'private_investigation_results': self.investigation_history.copy(),
                'investigation_priorities': self.investigation_priorities(analysis['players'], self.targets(state, 'investigate', me))}
        elif self.known.get(me) == 'werewolf':
            analysis['werewolf_team_analysis'] = {
                'known_teammates': sorted(p for p,r in self.known.items() if r == 'werewolf' and p != me),
                'nighttime_target_rankings': self.threat_scores(me, self.targets(state, 'kill', me), analysis['players'])}
        return analysis

    def report(self, me, public=False):
        known = self.public_known if public else self.known
        nonwolves = set() if public else self.nonwolves
        claims = self.public_claims if public else self.claims
        accusations = self.public_accusations if public else self.accusations
        defenses = self.public_defenses if public else self.defenses
        result = {}
        for p in sorted(self.players):
            if p == me and not public:
                continue
            evidence = []
            if len(claims[p]) > 1:
                evidence.append(('changed_claim', WEIGHTS['changed_claim']))
            for day, voter, target in sorted(self.votes):
                if voter != p:
                    continue
                r = known.get(target)
                if r == 'werewolf':
                    evidence.append((f'voted_known_wolf_day_{day}', WEIGHTS['voted_known_wolf']))
                elif r in ('seer', 'villager') or target in nonwolves:
                    evidence.append((f'voted_known_nonwolf_day_{day}', WEIGHTS['voted_known_nonwolf']))
            for target in sorted(defenses[p]):
                if known.get(target) == 'werewolf':
                    evidence.append((f'defended_known_wolf_{target}', WEIGHTS['defended_known_wolf']))
            score = max(-3, min(3, sum(w for _, w in evidence)))
            # Illustrative priors, not learned or calibrated. No assumed role counts.
            logits = {'werewolf': math.log(.25) + score,
                      'villager': math.log(.60), 'seer': math.log(.15) + self.seer_evidence(p, known, nonwolves, claims, accusations)}
            if p in nonwolves:
                logits['werewolf'] = -math.inf
            if p in known:
                probs = {r: float(r == known[p]) for r in ROLES}
            else:
                weights = {r: math.exp(v) for r, v in logits.items()}
                total = sum(weights.values())
                probs = {r: w / total for r, w in weights.items()}
            result[str(p)] = {'suspicion_score': round(100 * probs['werewolf'], 1),
                              'role_estimates': {r: round(v, 4) for r, v in probs.items()},
                              'possible_roles': [r for r, v in probs.items() if v > 0],
                              'confirmed_role': known.get(p),
                              'claims': sorted(claims[p]),
                              'accuses': sorted(accusations[p]),
                              'defends': sorted(defenses[p]),
                              'evidence': evidence}
        return {'warning': 'Heuristic estimates, not calibrated probabilities. Claims may be false. '
                'No joint role-count constraint. Missing fields mean missing evidence. '
                'Use your role and legal actions; do not automatically target highest suspicion.',
                'players': result}

    @staticmethod
    def seer_evidence(p, known, nonwolves, claims, accusations):
        correct = sum(known.get(t) == 'werewolf' for t in accusations[p])
        incorrect = sum(known.get(t) in ('villager','seer') or t in nonwolves for t in accusations[p])
        return max(-3, min(3, .8 * ('seer' in claims[p]) + 1.2 * correct - .8 * incorrect))

    def targets(self, state, verb, me):
        return {p for a in state.legal_actions
                if str(a.label).lower().startswith(verb)
                and (p := seat(a.action_id)) in self.players
                and p != me and p not in self.dead}

    def connectivity(self, p):
        connections = set(self.accusations[p]) | set(self.defenses[p])
        for store in (self.accusations, self.defenses):
            connections.update(sender for sender, targets in store.items() if p in targets)
        for _, voter, target in self.votes:
            if voter == p: connections.add(target)
            if target == p: connections.add(voter)
        return len(connections - {p}) / max(len(self.players)-1, 1)

    def investigation_priorities(self, profiles, targets):
        result = []
        for p in sorted(targets):
            if p in self.known or p in self.nonwolves or p in self.investigation_history:
                continue
            q = profiles[str(p)]['role_estimates']['werewolf']
            h = -q*math.log2(q)-(1-q)*math.log2(1-q) if 0 < q < 1 else 0
            c = self.connectivity(p)
            result.append(dict(player=p, werewolf_probability=q, information_gain=round(h,4), social_connectivity=round(c,4), investigation_priority=round(.35*q+.45*h+.20*c,4)))
        return sorted(result, key=lambda x: -x['investigation_priority'])

    def threat_scores(self, me, targets, profiles):
        wolves = {p for p,r in self.known.items() if r == 'werewolf'} | {me}
        result = []
        for p in sorted(targets - wolves):
            correct = self.accusations[p] & wolves
            incorrect = {t for t in self.accusations[p] if self.known.get(t) in ('villager','seer') or t in self.nonwolves} - wolves
            votes = [t for _,v,t in self.votes if v == p]
            a = len(correct)/max(len(wolves),1)
            v = sum(t in wolves for t in votes)/max(len(votes),1)
            # Conditional on target being nonwolf; does not assert full roster knowledge.
            probs = profiles[str(p)]['role_estimates']
            q = probs['seer']/max(probs['seer']+probs['villager'],1e-12)
            supporters = {s for s,ts in self.public_defenses.items() if p in ts and s not in wolves and s != p and s not in self.dead}
            accusers = {s for s,ts in self.public_accusations.items() if p in ts and s != p and s not in self.dead}
            n = max(len(self.players-self.dead)-1,1)
            c = min(1,len(supporters)/n)
            d = (len(correct)+1)/(len(correct)+len(incorrect)+2)
            l = 1-min(1,len(accusers)/n)
            score = 100*(.30*q+.20*a+.20*v+.15*c+.10*d+.05*l)
            result.append(dict(player=p, threat_score=round(score,2), seer_probability_given_nonwolf=round(q,4), accusation_score=round(a,4), voting_score=round(v,4), social_influence=round(c,4), analytical_accuracy=round(d,4), daytime_survival_proxy=round(l,4), accused_wolves=sorted(correct)))
        return sorted(result,key=lambda x:-x['threat_score'])
