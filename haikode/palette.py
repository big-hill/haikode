"""
The command palette and the searchable-list model behind every dialog.

opencode builds its model picker, provider picker, session list and command
palette on one generic component (ui/dialog-select.tsx): a fuzzy filter, a
cursor, page scrolling and category headers. This is that component ported as
a curses-free state machine, so the TUI stays a pure renderer and the part
that actually decides what a palette feels like -- the ranking -- can be unit
tested.

Two conventions are worth knowing before reading on:

  * A command's ``enabled()`` predicate decides whether it is *listed at all*,
    the way opencode's isVisiblePaletteCommand drops hidden commands. A command
    that is listed but whose handler could not be resolved becomes a *disabled*
    PaletteItem instead: a caller that forgot to supply one callable gets a
    greyed-out row, never an exception while the palette is opening. This is a
    deliberate departure -- dialog-select drops disabled options entirely
    (`filter((x) => x.disabled !== true)`) -- because on Haiku a missing
    handler means a feature is not wired up yet, and hiding it would make the
    palette look like the command does not exist.
  * Highlight positions always index into ``PaletteItem.title``, because that
    is the only string every dialog draws.
"""

from dataclasses import dataclass
from functools import lru_cache
from typing import (Any, Callable, Dict, Iterable, List, Mapping, Optional,
                    Sequence, Tuple)

# --------------------------------------------------------------------------
# fuzzy matching
# --------------------------------------------------------------------------

# Characters after which a match counts as starting a new word. opencode leans
# on fuzzysort for this; the set below covers the separators that actually
# occur in the strings we rank: titles, model ids, paths and provider names.
WORD_SEPARATORS = " \t-_/\\.:,()[]{}@+"

MATCH_SCORE = 16          # paid once per matched character
BOUNDARY_BONUS = 24       # match starts a word ("list" in "session list")
CAMEL_BONUS = 14          # camelCase / digit hump ("M" in "gptMini")
CONSECUTIVE_BONUS = 20    # match directly follows the previous match
FIRST_CHAR_BONUS = 12     # match begins at index 0
PREFIX_BONUS = 40         # the whole query is a literal prefix of the text
EXACT_BONUS = 60          # ... and the text is nothing else
WORD_BONUS = 30           # a matched run covers a whole word
GAP_START_PENALTY = 6     # cost of opening a gap between two matches
GAP_EXTEND_PENALTY = 2    # cost of each further skipped character
LEADING_GAP_PENALTY = 1   # per character skipped before the first match
LEADING_GAP_CAP = 20      # ... capped, so long strings stay comparable
TAIL_PENALTY = 1          # per unmatched character after the last match
TAIL_CAP = 20


@lru_cache(maxsize=2048)
def _bonuses(text: str) -> Tuple[int, ...]:
    """Positional bonus for every index of `text`; cached because the same
    titles are re-scored on every keystroke."""
    result: List[int] = []
    for index, char in enumerate(text):
        if index == 0:
            result.append(BOUNDARY_BONUS)
            continue
        previous = text[index - 1]
        if previous in WORD_SEPARATORS:
            result.append(BOUNDARY_BONUS)
        elif char.isupper() and previous.islower():
            result.append(CAMEL_BONUS)
        elif char.isdigit() and not previous.isdigit():
            result.append(CAMEL_BONUS)
        else:
            result.append(0)
    return tuple(result)


def _is_word(text: str, start: int, end: int) -> bool:
    """True when text[start:end + 1] is delimited by separators on both sides."""
    if start > 0 and text[start - 1] not in WORD_SEPARATORS:
        return False
    if end < len(text) - 1 and text[end + 1] not in WORD_SEPARATORS:
        return False
    return True


def _runs(positions: Sequence[int]) -> List[Tuple[int, int]]:
    """Group matched positions into contiguous (start, end) runs."""
    runs: List[Tuple[int, int]] = []
    start = last = positions[0]
    for position in positions[1:]:
        if position == last + 1:
            last = position
            continue
        runs.append((start, last))
        start = last = position
    runs.append((start, last))
    return runs


def _shape_bonus(term: str, text: str, positions: Sequence[int]) -> int:
    """Bonuses that depend on the whole match rather than a single character."""
    extra = 0
    if positions[0] == 0:
        extra += FIRST_CHAR_BONUS
        if list(positions) == list(range(len(term))):
            extra += PREFIX_BONUS
            if len(term) == len(text):
                extra += EXACT_BONUS
    # A single matched character only counts as a whole word when the query
    # itself is one character; otherwise "a b c" would out-rank "abc" for the
    # query "abc" by collecting three word bonuses.
    shortest = min(2, len(term))
    for start, end in _runs(positions):
        if end - start + 1 >= shortest and _is_word(text, start, end):
            extra += WORD_BONUS
    return extra


def _score_term(term: str, text: str) -> Optional[Tuple[int, List[int]]]:
    """
    Score one whitespace-free term against `text`.

    A Smith-Waterman style pass over (term x text): row `i` holds the best
    score for matching term[:i + 1] with term[i] landing on each index of the
    text, which lets consecutive runs and gaps be priced exactly instead of
    with the usual greedy left-to-right walk.
    """
    length = len(text)
    size = len(term)
    if size == 0 or size > length:
        return None

    lower = text.lower()
    needle = term.lower()
    bonuses = _bonuses(text)

    previous: List[Optional[int]] = [None] * length
    for index in range(length):
        if lower[index] != needle[0]:
            continue
        previous[index] = (MATCH_SCORE + bonuses[index]
                           - min(index, LEADING_GAP_CAP) * LEADING_GAP_PENALTY)
    if all(value is None for value in previous):
        return None

    parents: List[List[int]] = [[-1] * length]
    for i in range(1, size):
        current: List[Optional[int]] = [None] * length
        row = [-1] * length
        # Running maximum of previous[k] + GAP_EXTEND_PENALTY * k over k <= j-2,
        # which turns the "best predecessor across a gap" search into O(1):
        # the gap cost is linear in the distance, so the k-dependent part can
        # be folded into the maximum itself.
        gap_best: Optional[int] = None
        gap_index = -1
        for j in range(i, length):
            k = j - 2
            if k >= 0 and previous[k] is not None:
                candidate = previous[k] + GAP_EXTEND_PENALTY * k
                if gap_best is None or candidate > gap_best:
                    gap_best = candidate
                    gap_index = k
            if lower[j] != needle[i]:
                continue
            best: Optional[int] = None
            parent = -1
            adjacent = previous[j - 1]
            if adjacent is not None:
                best = adjacent + CONSECUTIVE_BONUS
                parent = j - 1
            if gap_best is not None:
                value = (gap_best - GAP_START_PENALTY
                         + GAP_EXTEND_PENALTY * (2 - j))
                if best is None or value > best:
                    best = value
                    parent = gap_index
            if best is None:
                continue
            current[j] = best + MATCH_SCORE + bonuses[j]
            row[j] = parent
        if all(value is None for value in current):
            return None
        previous = current
        parents.append(row)

    end = -1
    total: Optional[int] = None
    # Walk backwards so an equal score keeps the earliest end, i.e. the match
    # that leaves the shortest unmatched tail.
    for index in range(length - 1, -1, -1):
        value = previous[index]
        if value is None:
            continue
        if total is None or value >= total:
            total = value
            end = index

    positions = [end]
    cursor = end
    for i in range(size - 1, 0, -1):
        cursor = parents[i][cursor]
        positions.append(cursor)
    positions.reverse()

    score = int(total or 0)
    score += _shape_bonus(term, text, positions)
    score -= min(length - 1 - positions[-1], TAIL_CAP) * TAIL_PENALTY
    return score, positions


def fuzzy_score(query: str, text: str) -> Optional[Tuple[int, List[int]]]:
    """
    Rank `text` against `query` and report which characters matched.

    Returns (score, positions) or None when `query` is not a subsequence of
    `text`. Matching is case-insensitive; whitespace splits the query into
    terms that must all match (in any order), as in fzf. An empty query
    matches everything with score 0 and no positions, so callers can feed the
    unfiltered list through the same code path.
    """
    terms = (query or "").split()
    if not terms:
        return 0, []
    text = text or ""
    total = 0
    matched: set = set()
    for term in terms:
        result = _score_term(term, text)
        if result is None:
            return None
        total += result[0]
        matched.update(result[1])
    return total, sorted(matched)


# --------------------------------------------------------------------------
# items
# --------------------------------------------------------------------------

@dataclass
class PaletteItem:
    """
    One row of any dialog.

    `value` carries whatever the caller needs back on select (a session id, a
    (provider, model) tuple, a command id); everything else is presentation.
    `detail` is the secondary line under the row, `footer` the right-aligned
    tag, `keys` the shortcut hint opencode renders in the palette footer.
    """

    id: str
    title: str
    description: str = ""
    category: str = ""
    detail: str = ""
    footer: str = ""
    disabled: bool = False
    value: Any = None
    keys: str = ""


# Relative weights when a query is matched against several fields of an item.
# Users search by name, so the title dominates, exactly as in dialog-select.
TITLE_WEIGHT = 2
CATEGORY_WEIGHT = 1
DESCRIPTION_WEIGHT = 1

EMPTY_LIST_MESSAGE = "Nothing to show here"


def match_item(item: PaletteItem, query: str) -> Optional[Tuple[int, List[int]]]:
    """
    Score an item against a query.

    The title, the category and the description are all searchable so that
    "config" finds a command by its category, but the returned positions are
    always title positions -- that is the string the UI highlights.
    """
    if not (query or "").split():
        return 0, []
    total = 0
    positions: List[int] = []
    matched = False
    result = fuzzy_score(query, item.title)
    if result is not None:
        total += result[0] * TITLE_WEIGHT
        positions = result[1]
        matched = True
    for text, weight in ((item.category, CATEGORY_WEIGHT),
                         (item.description, DESCRIPTION_WEIGHT)):
        if not text:
            continue
        result = fuzzy_score(query, text)
        if result is not None:
            total += result[0] * weight
            matched = True
    if not matched:
        return None
    return total, positions


# --------------------------------------------------------------------------
# the shared dialog model
# --------------------------------------------------------------------------

class SelectList:
    """
    Filter, cursor and paging for a list of PaletteItems.

    Every dialog in the TUI owns one of these and does nothing but draw
    `visible` (or `grouped`) and forward keys to `move`/`page`/`home`/`end`.
    Disabled items stay listed so an unwired command still shows up (see the
    module docstring) but the cursor never rests on one.

    `matches` is ordered the way opencode's flat() is: ranked, then bucketed
    into category sections. `cursor`, `visible` and `grouped()` all agree on
    that one order.
    """

    def __init__(self, items: Iterable[PaletteItem], query: str = "",
                 page_size: int = 10):
        self._items: List[PaletteItem] = list(items)
        self._page_size = max(1, int(page_size))
        self._query = query or ""
        self._matches: List[Tuple[PaletteItem, List[int]]] = []
        self._cursor = 0
        self._refilter()

    # -- data --

    @property
    def items(self) -> List[PaletteItem]:
        return list(self._items)

    @items.setter
    def items(self, value: Iterable[PaletteItem]) -> None:
        self._items = list(value)
        self._refilter()

    @property
    def query(self) -> str:
        return self._query

    @query.setter
    def query(self, value: str) -> None:
        self._query = value or ""
        # opencode snaps the selection back to the top on every filter change
        # (the moveTo(0) effect in dialog-select). Merely clamping the old
        # index would leave the cursor parked on some item the new query
        # happened to rank into that slot.
        self._refilter(reset_cursor=True)

    @property
    def page_size(self) -> int:
        return self._page_size

    @page_size.setter
    def page_size(self, value: int) -> None:
        self._page_size = max(1, int(value))

    def _refilter(self, reset_cursor: bool = False) -> None:
        scored: List[Tuple[PaletteItem, int, List[int]]] = []
        for item in self._items:
            result = match_item(item, self._query)
            if result is None:
                continue
            scored.append((item, result[0], result[1]))
        # sort() is stable, so equal scores keep the caller's ordering -- the
        # tiebreak dialogs rely on for favourites/recents sections.
        scored.sort(key=lambda entry: -entry[1])
        # Then bucket by category, exactly as dialog-select does
        # (filtered -> groupBy -> flatMap): the cursor indexes the *grouped*
        # list, so the flat order kept here has to be the order the rows are
        # drawn in. Without this the highlight lands on the wrong row as soon
        # as ranking interleaves two categories.
        order: List[str] = []
        buckets: Dict[str, List[Tuple[PaletteItem, List[int]]]] = {}
        for item, _, found in scored:
            category = item.category or ""
            if category not in buckets:
                buckets[category] = []
                order.append(category)
            buckets[category].append((item, found))
        self._matches = [entry for category in order
                         for entry in buckets[category]]
        start = 0 if reset_cursor else self._cursor
        self._cursor = self._settle(start, 1, wrap=False)

    # -- cursor --

    def _enabled(self, index: int) -> bool:
        return not self._matches[index][0].disabled

    def _settle(self, index: int, direction: int, wrap: bool) -> int:
        """Nearest selectable index at or after `index`, travelling `direction`."""
        count = len(self._matches)
        if count == 0:
            return 0
        step = 1 if direction >= 0 else -1
        if wrap:
            index %= count
            for _ in range(count):
                if self._enabled(index):
                    return index
                index = (index + step) % count
            return index
        index = max(0, min(count - 1, index))
        forward = range(index, count if step > 0 else -1, step)
        for probe in forward:
            if self._enabled(probe):
                return probe
        backward = range(index, -1 if step > 0 else count, -step)
        for probe in backward:
            if self._enabled(probe):
                return probe
        return index

    @property
    def cursor(self) -> int:
        return self._cursor

    def move_to(self, index: int) -> Optional[PaletteItem]:
        """Put the cursor on `index`, clamped and settled onto a live item."""
        self._cursor = self._settle(index, 1, wrap=False)
        return self.selected

    def _step(self, delta: int, wrap: bool) -> Optional[PaletteItem]:
        if not self._matches:
            return None
        self._cursor = self._settle(self._cursor + delta, delta, wrap=wrap)
        return self.selected

    def move(self, delta: int) -> Optional[PaletteItem]:
        """
        Step the cursor.

        Single steps wrap around, as in opencode's dialog move(); larger jumps
        clamp, so a page key at the top of the list does not teleport the user
        to the bottom.
        """
        return self._step(delta, wrap=abs(delta) == 1)

    def page(self, delta: int) -> Optional[PaletteItem]:
        """Jump a whole page, always clamping -- a one-row page is still a
        page key, so it must not start wrapping the way move(1) does."""
        return self._step(delta * self._page_size, wrap=False)

    def home(self) -> Optional[PaletteItem]:
        self._cursor = self._settle(0, 1, wrap=False)
        return self.selected

    def end(self) -> Optional[PaletteItem]:
        self._cursor = self._settle(len(self._matches) - 1, -1, wrap=False)
        return self.selected

    def reset_cursor(self) -> None:
        """Back to the top; the TUI calls this when a dialog is reopened."""
        self._cursor = self._settle(0, 1, wrap=False)

    # -- views --

    @property
    def matches(self) -> List[Tuple[PaletteItem, List[int]]]:
        # The position lists are copied too: a renderer that trims them to the
        # visible width must not be able to corrupt the filter state.
        return [(item, list(found)) for item, found in self._matches]

    @property
    def count(self) -> int:
        return len(self._matches)

    @property
    def total(self) -> int:
        return len(self._items)

    @property
    def page_index(self) -> int:
        return self._cursor // self._page_size

    @property
    def page_count(self) -> int:
        if not self._matches:
            return 1
        return (len(self._matches) + self._page_size - 1) // self._page_size

    @property
    def visible(self) -> List[Tuple[PaletteItem, List[int]]]:
        """The current page as (item, highlight positions in item.title)."""
        start = self.page_index * self._page_size
        return [(item, list(found))
                for item, found in self._matches[start:start + self._page_size]]

    @property
    def selected(self) -> Optional[PaletteItem]:
        if not self._matches:
            return None
        item = self._matches[self._cursor][0]
        return None if item.disabled else item

    @property
    def selected_positions(self) -> List[int]:
        if not self._matches:
            return []
        return list(self._matches[self._cursor][1])

    def grouped(self) -> List[Tuple[str, List[PaletteItem]]]:
        """
        Matches split into (category, items) sections for header rendering.

        _refilter already laid the matches out in section order, so this only
        has to cut them at the category boundaries. Concatenating the sections
        therefore reproduces `matches` exactly, which is what lets a renderer
        draw headers and still trust `cursor` as a row index.
        """
        sections: List[Tuple[str, List[PaletteItem]]] = []
        for item, _ in self._matches:
            category = item.category or ""
            if not sections or sections[-1][0] != category:
                sections.append((category, []))
            sections[-1][1].append(item)
        return sections

    @property
    def empty_message(self) -> str:
        """What to draw instead of rows; empty string while there are rows."""
        if self._matches:
            return ""
        query = self._query.strip()
        if self._items and query:
            return 'No matches for "%s" - try fewer characters' % query
        return EMPTY_LIST_MESSAGE


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

# opencode hides the palette's own command from the palette; so do we. The id
# is the one keybind.ts maps command_list to, which is also what haikode's
# keybind.COMMAND_MAP publishes -- they have to agree or the palette lists
# itself.
COMMAND_PALETTE_COMMAND = "command.palette.show"

UNAVAILABLE_DETAIL = "not available here"


class CommandUnavailable(RuntimeError):
    """Raised by CommandPalette.run for a known but currently unusable command."""


def _always() -> bool:
    return True


@dataclass
class Command:
    """A palette entry: what to show, what to run, and when to show it."""

    id: str
    title: str
    description: str = ""
    category: str = ""
    handler: Optional[Callable[..., Any]] = None
    keys: str = ""
    enabled: Callable[[], bool] = _always
    hidden: bool = False
    detail: str = ""

    @property
    def available(self) -> bool:
        return self.handler is not None


class CommandPalette:
    """
    The ctrl+p registry.

    Registration order is the display order, which is also the category order
    `SelectList.grouped()` reports, so callers group by registering in groups.
    """

    def __init__(self) -> None:
        self._commands: Dict[str, Command] = {}

    # -- registration --

    def register(self, id: str, title: str, description: str = "",
                 category: str = "", handler: Optional[Callable[..., Any]] = None,
                 keys: str = "", enabled: Optional[Callable[[], bool]] = None,
                 hidden: bool = False, detail: str = "") -> Command:
        """
        Add or replace a command.

        `enabled` decides visibility (mirroring isVisiblePaletteCommand); a
        command with no `handler` is still listed, but disabled.
        """
        command = Command(id=id, title=title, description=description,
                          category=category, handler=handler, keys=keys,
                          enabled=enabled or _always, hidden=hidden,
                          detail=detail)
        self._commands[id] = command
        return command

    def unregister(self, id: str) -> None:
        self._commands.pop(id, None)

    def get(self, id: str) -> Optional[Command]:
        return self._commands.get(id)

    def ids(self) -> List[str]:
        return list(self._commands)

    def __contains__(self, id: object) -> bool:
        return id in self._commands

    def __len__(self) -> int:
        return len(self._commands)

    # -- listing --

    def _is_enabled(self, command: Command) -> bool:
        try:
            return bool(command.enabled())
        except Exception:
            # A broken predicate disables its own command instead of taking
            # the whole palette down with it.
            return False

    def _visible(self, command: Command) -> bool:
        if command.hidden or command.id == COMMAND_PALETTE_COMMAND:
            return False
        return self._is_enabled(command)

    def items(self) -> List[PaletteItem]:
        """Everything currently worth showing, in registration order."""
        items: List[PaletteItem] = []
        for command in self._commands.values():
            if not self._visible(command):
                continue
            detail = command.detail
            if not command.available and not detail:
                detail = UNAVAILABLE_DETAIL
            items.append(PaletteItem(
                id=command.id,
                title=command.title,
                description=command.description,
                category=command.category,
                detail=detail,
                footer=command.keys,
                disabled=not command.available,
                value=command.id,
                keys=command.keys,
            ))
        return items

    def select_list(self, query: str = "", page_size: int = 10) -> SelectList:
        return SelectList(self.items(), query=query, page_size=page_size)

    # -- dispatch --

    def run(self, id: str, *args: Any, **kwargs: Any) -> Any:
        """
        Run a command and return its result.

        Unknown id raises KeyError; a command that is registered but has no
        handler, or whose enabled() says no, raises CommandUnavailable. Hidden
        commands still run -- hidden only keeps them out of the list.
        """
        command = self._commands[id]
        if not command.available:
            raise CommandUnavailable("command %r has no handler" % id)
        if not self._is_enabled(command):
            raise CommandUnavailable("command %r is not enabled" % id)
        return command.handler(*args, **kwargs)


# --------------------------------------------------------------------------
# the default command set
# --------------------------------------------------------------------------

# (id, title, description, category, keys). Ids and key hints follow
# packages/tui/src/config/keybind.ts, where the leader is ctrl+x.
DEFAULT_COMMANDS: Tuple[Tuple[str, str, str, str, str], ...] = (
    ("session.new", "New session",
     "Start a fresh session in this directory", "Session", "<leader>n"),
    ("session.list", "List sessions",
     "Switch to another session", "Session", "<leader>l"),
    ("session.rename", "Rename session",
     "Give the current session a new title", "Session", "ctrl+r"),
    ("session.compact", "Compact session",
     "Summarise the history to free up context", "Session", "<leader>c"),
    ("session.undo", "Undo message",
     "Revert the last message and its edits", "Session", "<leader>u"),
    ("session.export", "Export session",
     "Write the transcript to a file", "Session", "<leader>x"),

    ("model.list", "List models",
     "Switch the active model", "Model", "<leader>m"),
    ("provider.list", "List providers",
     "Browse configured and available providers", "Model", "ctrl+a"),
    ("model.cycle_recent", "Cycle recent model",
     "Jump to the next recently used model", "Model", "f2"),

    ("provider.add", "Add provider",
     "Register a new provider endpoint", "Config", ""),
    ("provider.default", "Set default provider",
     "Choose the provider new sessions start with", "Config", ""),
    ("auth.login", "Login",
     "Store an API key for a provider", "Config", ""),
    ("auth.logout", "Logout",
     "Remove a stored API key", "Config", ""),
    ("permission.list", "Permissions",
     "Review what tools may do without asking", "Config", ""),

    ("status.view", "Status",
     "Setup, tokens and context usage", "View", "<leader>s"),
    ("tool.list", "Tools",
     "List the tools available to the agent", "View", ""),
    ("todo.list", "Todos",
     "Show the current todo list", "View", ""),
    ("help.show", "Help",
     "Show keybindings and commands", "View", ""),
    ("reasoning.toggle", "Toggle reasoning",
     "Show or hide model reasoning blocks", "View", ""),

    ("app.exit", "Exit", "Quit haikode", "App", "ctrl+c"),
)


def resolve_handler(context: Any, command_id: str) -> Optional[Callable[..., Any]]:
    """
    Find the callable a command should run in `context`.

    `context` is a dict or any object; both the dotted command id and its
    underscore form are accepted ("session.new" or "session_new") so callers
    can use whichever reads better. Anything missing or not callable resolves
    to None, which is what turns the row grey.
    """
    if context is None:
        return None
    for key in (command_id, command_id.replace(".", "_")):
        if isinstance(context, Mapping):
            value = context.get(key)
        else:
            value = getattr(context, key, None)
        if callable(value):
            return value
    return None


def build_default_palette(context: Any = None) -> CommandPalette:
    """
    Register the standard opencode-ish command set against `context`.

    `context` supplies the callables (see resolve_handler), which keeps this
    module free of UI and IO. Commands whose callable is missing are still
    listed, disabled, so the palette always shows the same map of the app.
    """
    palette = CommandPalette()
    for command_id, title, description, category, keys in DEFAULT_COMMANDS:
        palette.register(command_id, title, description, category,
                         resolve_handler(context, command_id), keys)
    return palette
