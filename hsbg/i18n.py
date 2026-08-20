"""Interface wording, in whatever language Hearthstone itself is running in.

The overlay sits on top of the game and should read like part of it: a Russian
client gets Russian labels, every other client gets English. Card names already
follow the game's locale (see :mod:`hsbg.carddb`) — this module covers the words
we write ourselves.

Only two languages are carried. Hearthstone ships in fifteen locales, but the
card data is what makes the other thirteen usable at all; for our own labels
English is the honest fallback, and guessing a half-translated German is worse
than not pretending.

    t = strings("enUS")
    t("status.tavern")                  -> "Tavern"
    t("odds.vs", name="Bob")            -> "Combat vs: Bob"
    t.plural(3, "turns")                -> "3 turns"
"""
from __future__ import annotations

RU = "ru"
EN = "en"


def language_of(locale: str) -> str:
    """Map a Hearthstone locale ("ruRU", "enUS", "deDE") onto our two."""
    return RU if (locale or "").lower().startswith("ru") else EN


# key -> {language: text}. Placeholders are ``str.format`` fields, so a
# translation may reorder them freely.
STRINGS: dict[str, dict[str, str]] = {
    # --- status line ------------------------------------------------------
    "status.waiting_hs":    {RU: "Ожидание Hearthstone…",
                             EN: "Waiting for Hearthstone…"},
    "status.waiting_match": {RU: "Ожидание матча Battlegrounds…",
                             EN: "Waiting for a Battlegrounds match…"},
    "status.other_match":   {RU: "Идёт не-Battlegrounds матч",
                             EN: "A non-Battlegrounds match is running"},
    "status.game_over":     {RU: "Матч завершён", EN: "Match over"},
    "status.combat":        {RU: "Бой", EN: "Combat"},
    "status.tavern":        {RU: "Таверна", EN: "Tavern"},

    # --- main bar ---------------------------------------------------------
    "bar.combat_pending":   {RU: "Бой · считаем…", EN: "Combat · crunching…"},
    "bar.combat_win":       {RU: "Бой · победа {win}%", EN: "Combat · win {win}%"},
    "bar.detail":           {RU: "ход {turn} · {life} hp · таверна {tier}",
                             EN: "turn {turn} · {life} hp · tavern {tier}"},

    # --- odds card --------------------------------------------------------
    "odds.combat":          {RU: "Бой", EN: "Combat"},
    "odds.vs":              {RU: "Бой против: {name}", EN: "Combat vs: {name}"},
    "odds.pending":         {RU: "считаем расклад…", EN: "crunching the odds…"},
    "odds.opening":         {RU: "расклад на начало боя",
                             EN: "odds at the start of the fight"},
    "odds.tie":             {RU: "ничья {tie}%", EN: "tie {tie}%"},
    "odds.damage":          {RU: "урон ~{avg} (макс {max}) · ±{margin}%",
                             EN: "damage ~{avg} (max {max}) · ±{margin}%"},
    "odds.lethal":          {RU: "☠︎ смертельно в {risk}% случаев",
                             EN: "☠︎ lethal in {risk}% of runs"},
    "odds.coverage":        {RU: "точность ~{coverage} · {cards}",
                             EN: "accuracy ~{coverage} · {cards}"},

    # --- panels -----------------------------------------------------------
    "section.leaderboard":  {RU: "таблица лобби", EN: "lobby table"},
    "section.opponents":    {RU: "столы оппонентов", EN: "opponent boards"},
    "section.pool":         {RU: "пул миньонов", EN: "minion pool"},
    "section.stats":        {RU: "статистика", EN: "statistics"},
    "section.history":      {RU: "бои этого матча", EN: "fights this match"},
    "section.heroes":       {RU: "выбор героя", EN: "hero choice"},

    "marks.on":             {RU: "? вкл", EN: "? on"},
    "marks.off":            {RU: "? выкл", EN: "? off"},

    "hero.personal":        {RU: "ваше среднее место {avg} · {games}",
                             EN: "your average place {avg} · {games}"},
    "hero.unplayed":        {RU: "вы им ещё не играли", EN: "not played yet"},
    "hero.global":          {RU: "глобально", EN: "global"},
    "hero.yours":           {RU: "ваше", EN: "yours"},

    "row.tier":             {RU: "т{tier}", EN: "T{tier}"},
    "row.me":               {RU: "вы", EN: "you"},
    "row.board":            {RU: "{minions} · ход {turn}",
                             EN: "{minions} · turn {turn}"},
    "row.unseen":           {RU: "не видели", EN: "not seen"},
    "row.hover_hint":       {RU: "наведи на строку — покажем стол",
                             EN: "hover a row to see the board"},
    "row.player":           {RU: "игрок {id}", EN: "player {id}"},

    # --- statistics -------------------------------------------------------
    "stat.avg_place":       {RU: "Ваше среднее место", EN: "Your average place"},
    "stat.combat_wins":     {RU: "Побед в боях", EN: "Combat wins"},
    "stat.with_hero":       {RU: "С героем {hero}", EN: "As {hero}"},
    "stat.global_place":    {RU: "Глобальное ср. место", EN: "Global avg. place"},

    # --- popups -----------------------------------------------------------
    "popup.board_turn":     {RU: "стол с хода {turn}", EN: "board from turn {turn}"},
    "popup.ago":            {RU: "{turns} назад", EN: "{turns} ago"},
    "popup.just_now":       {RU: "только что", EN: "just now"},
    "popup.pool_left":      {RU: "в пуле осталось ~{left} из {total}",
                             EN: "~{left} of {total} left in the pool"},
    "popup.pool_seen":      {RU: "замечено на столах: {seen}",
                             EN: "seen on boards: {seen}"},
    "popup.minion":         {RU: "таверна {tier} · {attack}/{health}",
                             EN: "tavern {tier} · {attack}/{health}"},
    # Bob also sells spells; they have no stats and no minion pool behind them.
    "popup.spell":          {RU: "таверна {tier} · заклинание",
                             EN: "tavern {tier} · spell"},

    # --- history ----------------------------------------------------------
    "history.line":         {RU: "{mark} ход {turn} · {name}{damage}",
                             EN: "{mark} turn {turn} · {name}{damage}"},

    # --- warnings ---------------------------------------------------------
    "logcfg.updated":       {RU: "log.config обновлён — перезапусти Hearthstone,"
                                 " чтобы игра начала писать нужные секции.",
                             EN: "log.config was updated — restart Hearthstone once"
                                 " so the game starts writing the required sections."},
    "logcfg.ok":            {RU: "в log.config уже есть всё, что нужно.",
                             EN: "log.config already has everything we need."},
    "warn.fullscreen":      {RU: "Полноэкранный режим — включи оконный",
                             EN: "Fullscreen mode — switch the game to windowed"},
    "warn.no_logs":         {RU: "Логи Hearthstone не найдены",
                             EN: "Hearthstone logs not found"},
    "warn.card_db":         {RU: "Не удалось загрузить базу карт: {error}",
                             EN: "Could not load the card database: {error}"},
    "warn.journal":         {RU: "Журнал отладки: {error}",
                             EN: "Debug journal: {error}"},

    # --- menu bar item ----------------------------------------------------
    "menu.toggle_hidden":   {RU: "Свернуть / развернуть", EN: "Collapse / expand"},
    "menu.marks":           {RU: "Знаки «?» у оппонентов",
                             EN: "“?” pins on opponents"},
    "menu.recompute":       {RU: "Пересчитать бой", EN: "Recompute the fight"},
    "menu.debug_log":       {RU: "Отладка: журнал боёв", EN: "Debug: combat journal"},
    "menu.debug_folder":    {RU: "Открыть папку журнала", EN: "Open the journal folder"},
    "menu.quit":            {RU: "Выход", EN: "Quit"},
    "log.journal_on":       {RU: "журнал боёв включён", EN: "combat journal on"},
    "log.journal_off":      {RU: "журнал боёв выключен", EN: "combat journal off"},
    "log.hover_error":      {RU: "ошибка отслеживания курсора — {error}",
                             EN: "cursor tracking failed — {error}"},

    # --- launcher window --------------------------------------------------
    "launcher.title":       {RU: "Оверлей Hearthstone Battlegrounds",
                             EN: "Hearthstone Battlegrounds Overlay"},
    "launcher.start":       {RU: "Запустить", EN: "Start"},
    "launcher.stop":        {RU: "Остановить", EN: "Stop"},
    "launcher.starting":    {RU: "Запускается…", EN: "Starting…"},
    "launcher.stopping":    {RU: "Останавливается…", EN: "Stopping…"},
    "launcher.check":       {RU: "Проверить окружение", EN: "Check environment"},
    "launcher.log_caption": {RU: "Журнал — {path}", EN: "Log — {path}"},
    "launcher.running":     {RU: "● Оверлей работает — значок BG в меню-баре",
                             EN: "● Overlay is running — BG icon in the menu bar"},
    "launcher.stopped":     {RU: "○ Оверлей остановлен", EN: "○ Overlay stopped"},
    "launcher.empty_log":   {RU: "Журнал пуст. Нажми «Запустить».",
                             EN: "The log is empty. Press “Start”."},
    "launcher.menu_hide":   {RU: "Скрыть {app}", EN: "Hide {app}"},
    "launcher.menu_close":  {RU: "Закрыть окно", EN: "Close window"},
    "launcher.menu_quit":   {RU: "Выход из {app}", EN: "Quit {app}"},
    "launcher.banner_start": {RU: "запуск", EN: "start"},
    "launcher.banner_check": {RU: "проверка окружения", EN: "environment check"},
    "log.poll_error":       {RU: "ошибка опроса — {error}",
                             EN: "status poll failed — {error}"},

    # --- command line -----------------------------------------------------
    "cli.description":      {RU: "Оверлей для Hearthstone Battlegrounds",
                             EN: "Overlay for Hearthstone Battlegrounds"},
    "cli.check":            {RU: "проверить окружение и выйти",
                             EN: "check the environment and exit"},
    "cli.headless":         {RU: "без окна, вывод в консоль",
                             EN: "no window, print to the console"},
    "cli.launcher":         {RU: "окно с кнопкой запуска вместо самого оверлея",
                             EN: "the window with a start button instead of the overlay"},
    "cli.overlay":          {RU: "сам оверлей (по умолчанию в терминале)",
                             EN: "the overlay itself (the default in a terminal)"},
    "cli.iterations":       {RU: "симуляций на прогноз",
                             EN: "simulations per prediction"},
    "cli.version":          {RU: "показать версию и выйти",
                             EN: "print the version and exit"},

    "check.header":         {RU: "— Проверка окружения —", EN: "— Environment check —"},
    "check.log_dir":        {RU: "папка логов", EN: "log folder"},
    "check.log_config":     {RU: "log.config", EN: "log.config"},
    "check.language":       {RU: "язык игры", EN: "game language"},
    "check.fullscreen":     {RU: "полноэкранный режим", EN: "fullscreen"},
    "check.card_db":        {RU: "база карт", EN: "card database"},
    "check.hs_running":     {RU: "Hearthstone запущен", EN: "Hearthstone running"},
    "check.hs_window":      {RU: "окно игры", EN: "game window"},
    "check.appkit":         {RU: "AppKit", EN: "AppKit"},
    "check.not_found":      {RU: "НЕ НАЙДЕНА", EN: "NOT FOUND"},
    "check.setting":        {RU: "{value} (настройка: {setting})",
                             EN: "{value} (setting: {setting})"},
    "check.fullscreen_yes": {RU: "ДА — оверлей не будет виден",
                             EN: "YES — the overlay will not be visible"},
    "check.no":             {RU: "нет", EN: "no"},
    "check.yes":            {RU: "да", EN: "yes"},
    "check.cards":          {RU: "{cards} карт, эффектов извлечено: {effects}",
                             EN: "{cards} cards, {effects} effects derived"},
    "check.db_error":       {RU: "ОШИБКА {error}", EN: "ERROR {error}"},
    "check.window_missing": {RU: "не найдено", EN: "not found"},
    "check.appkit_missing": {RU: "недоступен ({error})", EN: "unavailable ({error})"},

    "headless.banner":      {RU: "Headless-режим. Ctrl+C для выхода.",
                             EN: "Headless mode. Ctrl+C to quit."},
    "headless.line":        {RU: "[{status}] ход {turn} · {health}hp · таверна {tier}",
                             EN: "[{status}] turn {turn} · {health}hp · tavern {tier}"},
    "headless.pending":     {RU: "расклад считается…", EN: "crunching the odds…"},
    "headless.odds":        {RU: "{headline} — победа {win}% / ничья {tie}%"
                                 " / поражение {loss}%  (урон ~{damage})",
                             EN: "{headline} — win {win}% / tie {tie}%"
                                 " / loss {loss}%  (damage ~{damage})"},
    "headless.no_board":    {RU: "стол не виден", EN: "board not seen"},
}

# Counted nouns. Russian takes three forms (1 ход, 2 хода, 5 ходов), English
# two — and both are needed in the same sentences.
PLURALS: dict[str, dict[str, tuple[str, ...]]] = {
    "turns":   {RU: ("ход", "хода", "ходов"), EN: ("turn", "turns")},
    "games":   {RU: ("игра", "игры", "игр"), EN: ("game", "games")},
    "fights":  {RU: ("бой", "боя", "боёв"), EN: ("fight", "fights")},
    "minions": {RU: ("мин", "мин", "мин"), EN: ("minion", "minions")},
}


def _russian_form(n: int, forms: tuple[str, ...]) -> str:
    tail_two, tail_one = n % 100, n % 10
    if 11 <= tail_two <= 14 or tail_one == 0 or tail_one >= 5:
        return forms[2]
    if tail_one == 1:
        return forms[0]
    return forms[1]


class Strings:
    """The label vocabulary for one language."""

    def __init__(self, language: str = EN):
        self.language = language if language in (RU, EN) else EN

    def __call__(self, key: str, **fields) -> str:
        entry = STRINGS.get(key)
        if entry is None:
            return key          # a typo shows up on screen instead of crashing
        text = entry.get(self.language) or entry[EN]
        return text.format(**fields) if fields else text

    def plural(self, n: int, noun: str) -> str:
        """``5`` + ``"turns"`` -> "5 ходов" / "5 turns"."""
        forms = PLURALS[noun][self.language]
        word = _russian_form(n, forms) if self.language == RU \
            else (forms[0] if n == 1 else forms[1])
        return f"{n} {word}"


_CACHE: dict[str, Strings] = {}


def strings(locale: str) -> Strings:
    """Labels for a Hearthstone locale — ``"ruRU"``, ``"enUS"``, and so on."""
    language = language_of(locale)
    if language not in _CACHE:
        _CACHE[language] = Strings(language)
    return _CACHE[language]
