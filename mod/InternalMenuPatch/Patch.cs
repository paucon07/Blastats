using HarmonyLib;
using Il2CppTMPro;
using MelonLoader;
using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Text;
using UnityEngine;
using UnityEngine.EventSystems;
using UnityEngine.UI;

[assembly: MelonInfo(typeof(InternalMenuPatch), "InternalMenuPatch", "1.0.0", "xyfume (original idea by june !)")]
[assembly: MelonGame(null, null)]

public class InternalMenuPatch : MelonMod
{
    // shared state
    public static object HandlerInstance; // live internal mode menu handler
    public static TextMeshProUGUI ButtonText;
    public static Color NormalColor;
    public static float LastSelectRoomCallTime = 0f; // last time the queue picked a room

    internal const BindingFlags F = BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance;

    // build Il2CppObjectBase.Cast<T> for a type only known at runtime
    internal static MethodInfo MakeCast(Type t)
    {
        if (t == null) return null;
        return typeof(Il2CppInterop.Runtime.InteropTypes.Il2CppObjectBase)
            .GetMethod("Cast", BindingFlags.Public | BindingFlags.Instance)
            ?.MakeGenericMethod(t);
    }

    // mod startup
    public override void OnInitializeMelon()
    {
        HarmonyInstance.PatchAll();
        BlastonTracker.Init();
    }

    // runs on every scene load but button setup and the watchdog only start once
    public override void OnSceneWasLoaded(int buildIndex, string sceneName)
    {
        MelonCoroutines.Start(SetupButton());

        if (!watchdogStarted)
        {
            watchdogStarted = true;
            MelonCoroutines.Start(Watchdog());
        }
    }

    // harmony patches
    // grab the menu handler when it initialises and send it straight into the queue
    [HarmonyPatch]
    public class HandlerInitPatch
    {
        static MethodBase TargetMethod()
        {
            var t = AccessTools.TypeByName("Il2CppIgnis.AutoSpectateInternalModeMenuViewHandler");
            return t == null ? null : AccessTools.Method(t, "Initialize", new[] { typeof(Transform) });
        }

        static void Postfix(object __instance)
        {
            HandlerInstance = __instance;
            try
            {
                var method = __instance.GetType().GetMethod("VisitQueue", F);
                if (method == null) { BlastonTracker.Log("VisitQueue not found, cant auto queue"); return; }
                method.Invoke(__instance, null);
                BlastonTracker.Log("joined spectate queue");
            }
            catch (Exception e)
            {
                BlastonTracker.Log($"auto queue error: {e.Message}");
            }
        }
    }

    // drop queue picks where neither player is at the lp threshold
    [HarmonyPatch]
    public class SelectRoomFilterPatch
    {
        private const int LP_THRESHOLD = 1900;
        private static string lastAccepted = null; // the queue can pick the same room repeatedly
        private static Type announcementType;
        private static MethodInfo announcementCast;

        static MethodBase TargetMethod()
        {
            var t = AccessTools.TypeByName("Il2CppIgnis.AutoSpectateQueueHandler");
            if (t == null) { BlastonTracker.Log("AutoSpectateQueueHandler not found, lp filter disabled"); return null; }
            return AccessTools.Method(t, "SelectRoomToSpectate");
        }

        static void Postfix(ref object __result)
        {
            // any call at all means the queue is alive
            LastSelectRoomCallTime = Time.realtimeSinceStartup;
            if (__result == null) return;

            try
            {
                if (announcementType == null)
                {
                    announcementType = AccessTools.TypeByName("Il2CppIgnis.MatchStartAnnouncementItem");
                    if (announcementType == null) return;
                    announcementCast = MakeCast(announcementType);
                }

                var typed = announcementCast?.Invoke(__result, null);
                if (typed == null) return;

                var p1Lp = (int)announcementType.GetProperty("playerOneLp", F).GetValue(typed);
                var p2Lp = (int)announcementType.GetProperty("playerTwoLp", F).GetValue(typed);
                var p1Name = announcementType.GetProperty("playerOneName", F).GetValue(typed)?.ToString() ?? "?";
                var p2Name = announcementType.GetProperty("playerTwoName", F).GetValue(typed)?.ToString() ?? "?";

                bool accept = p1Lp >= LP_THRESHOLD || p2Lp >= LP_THRESHOLD;
                // log each accepted pairing once, rejects are too frequent to log
                var sig = $"{p1Name}_{p2Name}";
                if (accept && sig != lastAccepted)
                {
                    BlastonTracker.Log($"spectating {p1Name} ({p1Lp}) vs {p2Name} ({p2Lp})");
                    lastAccepted = sig;
                }

                // only stash ids on accept
                // a rejected pick mid match would otherwise overwrite the match being recorded
                if (accept)
                {
                    BlastonTracker.PendingMatchId = announcementType.GetProperty("matchId", F)?.GetValue(typed)?.ToString();
                    BlastonTracker.PendingRegion = announcementType.GetProperty("region", F)?.GetValue(typed)?.ToString();
                }
                else __result = null;
            }
            catch (Exception e)
            {
                BlastonTracker.Log($"lp filter error: {e.Message}");
            }
        }
    }

    // auto spectate settings
    private static bool settingsPatched = false;

    // force the spectate settings we want on every settings instance
    private static void PatchAutoSpectateSettings()
    {
        if (settingsPatched) return;

        var settingsType = AccessTools.TypeByName("Il2CppIgnis.AutoSpectateSettings");
        if (settingsType == null) { BlastonTracker.Log("AutoSpectateSettings not found, settings not patched"); return; }

        var all = Resources.FindObjectsOfTypeAll(Il2CppInterop.Runtime.Il2CppType.From(settingsType));
        if (all == null || all.Length == 0) return;

        var propsToSet = new Dictionary<string, bool>
        {
            { "interruptMatches", true },
            { "prioritiseHigherRankedPlayers", true },
            { "spectateShootoutDuelMatches", false },
            { "spectateWinterDuelMatches", false },
            { "spectateStreetBrawlMatches", false },
        };

        // resolve every property up front, bail if any are missing
        var props = new Dictionary<string, PropertyInfo>();
        foreach (var name in propsToSet.Keys)
        {
            var p = settingsType.GetProperty(name, F);
            if (p == null) { BlastonTracker.Log($"settings property {name} missing, settings not patched"); return; }
            props[name] = p;
        }

        var castMethod = MakeCast(settingsType);
        if (castMethod == null) { BlastonTracker.Log("Cast<T> not found, settings not patched"); return; }

        int patched = 0;
        foreach (var obj in all)
        {
            try
            {
                var typed = castMethod.Invoke(obj, null);
                if (typed == null) continue;
                foreach (var kvp in propsToSet)
                    props[kvp.Key].SetValue(typed, kvp.Value);
                patched++;
            }
            catch (Exception e)
            {
                BlastonTracker.Log($"settings patch error: {e.Message}");
            }
        }

        if (patched > 0)
        {
            settingsPatched = true;
            BlastonTracker.Log($"spectate settings patched on {patched} instances");
        }
    }

    // setup
    private static bool setupRun = false;

    // wait for the public menu to exist then patch settings and add the button
    private IEnumerator SetupButton()
    {
        if (setupRun) yield break;
        setupRun = true;

        GameObject worldParent = null;
        while (worldParent == null) { worldParent = GameObject.Find("WorldParent"); yield return null; }

        PatchAutoSpectateSettings();

        Transform publicMenu = null;
        while (publicMenu == null) { publicMenu = worldParent.transform.Find("AutoSpectatePublicModeMenuReference(Clone)"); yield return null; }

        Transform canvas = null;
        while (canvas == null) { canvas = publicMenu.Find("Canvas"); yield return null; }

        CreateButton(canvas);
    }

    // watchdog
    private static bool watchdogStarted = false;

    private static Type cachedHandlerType;
    private static Type cachedDataType;
    private static PropertyInfo cachedStateProp;
    private static MethodInfo cachedHandlerCast;
    private static MethodInfo cachedDataCast;
    private static PropertyInfo cachedDataProp;
    private static FieldInfo cachedDataField;

    // resolve the reflection needed to read the spectate state once
    private static bool TryInitWatchdogReflection()
    {
        if (cachedStateProp != null) return true;

        cachedHandlerType = AccessTools.TypeByName("Il2CppIgnis.AutoSpectateInternalModeMenuViewHandler");
        cachedDataType = AccessTools.TypeByName("Il2CppIgnis.AutoSpectateData");
        if (cachedHandlerType == null || cachedDataType == null) return false;

        cachedStateProp = cachedDataType.GetProperty("state", F);
        if (cachedStateProp == null) return false;

        // autoSpectateData can be a property or a field depending on the build
        cachedDataProp = cachedHandlerType.GetProperty("autoSpectateData", F);
        cachedDataField = cachedHandlerType.GetField("autoSpectateData", F);
        if (cachedDataProp == null && cachedDataField == null) return false;

        cachedHandlerCast = MakeCast(cachedHandlerType);
        cachedDataCast = MakeCast(cachedDataType);

        return cachedHandlerCast != null && cachedDataCast != null;
    }

    // read the current auto spectate state name or null if unavailable
    private static string GetCurrentState()
    {
        if (HandlerInstance == null) return null;
        if (!TryInitWatchdogReflection()) return null;

        try
        {
            var typedHandler = cachedHandlerCast.Invoke(HandlerInstance, null);
            if (typedHandler == null) return null;

            var rawData = cachedDataProp != null
                ? cachedDataProp.GetValue(typedHandler)
                : cachedDataField.GetValue(typedHandler);
            if (rawData == null) return null;

            var typedData = cachedDataCast.Invoke(rawData, null);
            return cachedStateProp.GetValue(typedData)?.ToString();
        }
        catch
        {
            return null;
        }
    }

    // sample the state every few seconds and restart the game if it looks stuck
    private static IEnumerator Watchdog()
    {
        const float SAMPLE_INTERVAL = 5f;
        const float STUCK_NONSAFE_SECONDS = 60f;
        const float MAX_MATCH_DURATION = 600f;
        const float QUEUE_DEAD_SECONDS = 300f;

        var safeStates = new HashSet<string> { "LockerRoom", "Dormant" };

        // wait until the handler exists
        while (GetCurrentState() == null)
            yield return new WaitForSecondsRealtime(SAMPLE_INTERVAL);

        BlastonTracker.Log("watchdog started");

        string lastState = null;
        float stateEnteredAt = Time.realtimeSinceStartup;

        while (true)
        {
            yield return new WaitForSecondsRealtime(SAMPLE_INTERVAL);

            try
            {
                var state = GetCurrentState();
                if (state == null) continue;

                // new state so restart the timer
                if (state != lastState)
                {
                    lastState = state;
                    stateEnteredAt = Time.realtimeSinceStartup;
                    continue;
                }

                float elapsed = Time.realtimeSinceStartup - stateEnteredAt;

                // match running far longer than any real match
                if (state == "SpectatingMatch" && elapsed > MAX_MATCH_DURATION)
                {
                    BlastonTracker.Log($"match running over {MAX_MATCH_DURATION:F0}s, restarting");
                    RestartGame();
                    yield break;
                }

                // sat in the locker room but the queue stopped picking rooms
                if (state == "LockerRoom" && LastSelectRoomCallTime > 0)
                {
                    float sinceLastCall = Time.realtimeSinceStartup - LastSelectRoomCallTime;
                    if (sinceLastCall > QUEUE_DEAD_SECONDS)
                    {
                        BlastonTracker.Log($"queue dead for {sinceLastCall:F0}s, restarting");
                        RestartGame();
                        yield break;
                    }
                }

                // stuck in a transition state
                if (!safeStates.Contains(state) && state != "SpectatingMatch" && elapsed > STUCK_NONSAFE_SECONDS)
                {
                    BlastonTracker.Log($"stuck in {state} for {elapsed:F0}s, restarting");
                    RestartGame();
                    yield break;
                }
            }
            catch (Exception e)
            {
                BlastonTracker.Log($"watchdog error: {e.Message}");
            }
        }
    }

    // launch a fresh copy of the game then quit this one
    private static void RestartGame()
    {
        try
        {
            string exePath = Process.GetCurrentProcess().MainModule.FileName;
            Process.Start(new ProcessStartInfo
            {
                FileName = exePath,
                UseShellExecute = true,
                WorkingDirectory = Path.GetDirectoryName(exePath)
            });
            Application.Quit();
        }
        catch (Exception e)
        {
            BlastonTracker.Log($"restart error: {e.Message}");
            Application.Quit();
        }
    }

    // button
    // add the internal menu button to the public menu canvas
    private void CreateButton(Transform parent)
    {
        GameObject btnObj = new GameObject("InternalModeSwapButton");
        btnObj.transform.SetParent(parent, false);

        // invisible hit area
        Image btnImage = btnObj.AddComponent<Image>();
        btnImage.color = new Color(1.0f, 1.0f, 1.0f, 0.0f);

        Button btn = btnObj.AddComponent<Button>();

        RectTransform rect = btnObj.GetComponent<RectTransform>();
        rect.sizeDelta = new Vector2(220, 50);
        rect.anchoredPosition = new Vector2(-780, -355);

        GameObject textObj = new GameObject("Text");
        textObj.transform.SetParent(btnObj.transform, false);

        TextMeshProUGUI tmpText = textObj.AddComponent<TextMeshProUGUI>();
        tmpText.text = "INTERNAL MENU";
        tmpText.alignment = TextAlignmentOptions.Center;
        tmpText.fontSize = 24;

        Color normalColor = new Color(1.0f, 1.0f, 1.0f);
        Color hoverColor = new Color(0.945f, 0.737f, 0.196f, 1.0f);
        tmpText.color = normalColor;

        ButtonText = tmpText;
        NormalColor = normalColor;

        // match the games menu font
        foreach (var f in Resources.FindObjectsOfTypeAll<TMP_FontAsset>())
        {
            if (f.name.Contains("ShareTechMono")) { tmpText.font = f; break; }
        }

        textObj.GetComponent<RectTransform>().sizeDelta = rect.sizeDelta;

        // hover colour
        EventTrigger trigger = btnObj.AddComponent<EventTrigger>();

        var entryHover = new EventTrigger.Entry { eventID = EventTriggerType.PointerEnter };
        entryHover.callback.AddListener(new Action<BaseEventData>(_ => tmpText.color = hoverColor));
        trigger.triggers.Add(entryHover);

        var entryExit = new EventTrigger.Entry { eventID = EventTriggerType.PointerExit };
        entryExit.callback.AddListener(new Action<BaseEventData>(_ => tmpText.color = normalColor));
        trigger.triggers.Add(entryExit);

        btn.onClick.AddListener(new Action(OnClick));
    }

    // open the duel selector on the internal menu handler
    private void OnClick()
    {
        if (ButtonText != null) ButtonText.color = NormalColor;
        if (HandlerInstance == null) return;

        var method = HandlerInstance.GetType().GetMethod("VisitDuelSelectorMenu", F);
        method?.Invoke(HandlerInstance, null);
    }
}

// match tracker
public static class BlastonTracker
{
    // blastats data folder that api.py and the poller read from
    // set in UserData/MelonPreferences.cfg
    private static string DataDir;
    private static string LogPath;
    private static string AvatarDir; // served at /avatars/{sprite}.png

    // read the data folder from melon preferences
    public static void Init()
    {
        var cat = MelonPreferences.CreateCategory("BlastonTracker");
        DataDir = cat.CreateEntry("DataDir", "").Value;
        if (string.IsNullOrWhiteSpace(DataDir))
        {
            DataDir = null;
            Log("DataDir not set in UserData/MelonPreferences.cfg, matches wont be saved");
            return;
        }
        LogPath = Path.Combine(DataDir, "matches.json");
        AvatarDir = Path.Combine(DataDir, "avatars");
        Directory.CreateDirectory(DataDir);
        Log($"tracker started, writing to {LogPath}");
    }

    const BindingFlags F = BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance;

    private const int MAX_HEALTH = 100;

    // match in progress, null between matches
    private static MatchRecord currentMatch;

    // cross patch state
    // set by the room filter on accept, used when the match saves then cleared
    public static string PendingMatchId = null;
    public static string PendingRegion = null;

    // live PlayerHealthHandler, only reachable through the CleanUpRound hook
    public static object HealthHandlerInstance = null;

    // round state
    // end of round damage taken per player, latched on the first CleanUpRound
    private static int pendingDmg0 = 0, pendingDmg1 = 0;
    private static bool roundDamageLatched = false;

    // CleanUpRound fires several times per round end so stats are only captured once
    private static bool roundStatsCaptured = false;

    // player id to Damage, Grab or Collision to weapon to value
    public static Dictionary<int, Dictionary<string, Dictionary<string, double>>> pendingRoundStats
        = new Dictionary<int, Dictionary<string, Dictionary<string, double>>>();

    // raw damage hits for the round, deduped and rebased at round end
    public struct DamageEvent { public float TimeRaw; public string Weapon; public int Receiver; public float Damage; }
    private static readonly List<DamageEvent> pendingRoundEvents = new List<DamageEvent>();

    // Time.time at round end, anchors the timeline
    private static float pendingRoundEndRealtime = 0f;

    // note: per weapon damage dealt and grab dedupe would both need a hook on a Tool method
    // any detour on a Tool method crashes the spectator client

    // helpers
    // print a tagged line to the melon console
    public static void Log(string msg) => MelonLogger.Msg("[BlastonTracker] " + msg);

    // value as a string, or <TypeName> for objects
    public static string Describe(object v)
    {
        if (v == null) return "null";
        var t = v.GetType();
        if (v is string || t.IsPrimitive || t.IsEnum) return v.ToString();
        return $"<{t.Name}>";
    }

    // pull items out of any il2cpp list or collection
    // fast path for List<T>, enumerator for everything else like dict key sets
    private static List<object> EnumerateAny(object coll, int max = 24)
    {
        var outp = new List<object>();
        if (coll == null) return outp;
        try
        {
            var t = coll.GetType();
            var countProp = t.GetProperty("Count");
            var getItem = t.GetMethod("get_Item", new[] { typeof(int) });
            if (countProp != null && getItem != null)
            {
                int n = (int)countProp.GetValue(coll);
                for (int i = 0; i < n && i < max; i++)
                    try { outp.Add(getItem.Invoke(coll, new object[] { i })); } catch { }
                return outp;
            }
            var getEnum = t.GetMethod("GetEnumerator", Type.EmptyTypes);
            if (getEnum != null)
            {
                var en = getEnum.Invoke(coll, null);
                var et = en.GetType();
                var moveNext = et.GetMethod("MoveNext");
                var currentProp = et.GetProperty("Current");
                int guard = 0;
                while (guard++ < max && (bool)moveNext.Invoke(en, null))
                    outp.Add(currentProp.GetValue(en));
            }
        }
        catch { }
        return outp;
    }

    // escape a string for the hand written json
    private static string J(string s)
    {
        if (string.IsNullOrEmpty(s)) return "";
        var sb = new StringBuilder(s.Length + 8);
        foreach (char c in s)
        {
            switch (c)
            {
                case '\\': sb.Append("\\\\"); break;
                case '"': sb.Append("\\\""); break;
                case '\n': sb.Append("\\n"); break;
                case '\r': sb.Append("\\r"); break;
                case '\t': sb.Append("\\t"); break;
                default:
                    if (c < 0x20) sb.Append("\\u").Append(((int)c).ToString("x4"));
                    else sb.Append(c);
                    break;
            }
        }
        return sb.ToString();
    }

    // scoreboard lookup
    private static Il2CppIgnis.AutoSpectateScoreboardReference cachedScoreboard;

    // find the scoreboard once and refind only after it gets destroyed
    public static Il2CppIgnis.AutoSpectateScoreboardReference GetScoreboardReference()
    {
        if (cachedScoreboard == null)
            cachedScoreboard = GameObject.FindObjectOfType<Il2CppIgnis.AutoSpectateScoreboardReference>();
        return cachedScoreboard;
    }

    // round capture
    // read damage taken for both players off the health handler
    // index 0 and 1 line up with network id 1 and 2
    private static double[] ReadDamageTaken(object handler)
    {
        try
        {
            var list = handler.GetType().GetProperty("playerLastRecordedDamage", F)?.GetValue(handler);
            if (list == null) return null;
            var lt = list.GetType();
            int count = (int)(lt.GetProperty("Count")?.GetValue(list) ?? 0);
            if (count < 2) return null;
            var getItem = lt.GetMethod("get_Item", new[] { typeof(int) });
            double d0 = Convert.ToDouble(getItem.Invoke(list, new object[] { 0 }));
            double d1 = Convert.ToDouble(getItem.Invoke(list, new object[] { 1 }));
            return new[] { d0, d1 };
        }
        catch (Exception e) { Log($"read damage error: {e.Message}"); return null; }
    }

    // copy roundRecords before the game resets it
    // shape is roundRecords[playerId][Grab|Damage|Collision][weapon] = value
    public static void CaptureRoundStats(object healthHandler)
    {
        try
        {
            pendingRoundStats.Clear();
            var drk = healthHandler.GetType().GetProperty("damageRecordKeeper", F)?.GetValue(healthHandler);
            var gdt = drk?.GetType().GetProperty("gameDesignTrackingData", F)?.GetValue(drk);
            var rr = gdt?.GetType().GetProperty("roundRecords", F)?.GetValue(gdt);
            if (rr == null) return;

            var rrT = rr.GetType();
            var getPlayer = rrT.GetMethod("get_Item");
            foreach (var pk in EnumerateAny(rrT.GetProperty("Keys")?.GetValue(rr), 8))
            {
                int pid;
                if (!int.TryParse(Describe(pk), out pid)) continue;
                if (pid < 0) continue; // -1 is our spectator seat
                var byType = getPlayer?.Invoke(rr, new[] { pk });
                if (byType == null) continue;

                var btT = byType.GetType();
                var getType = btT.GetMethod("get_Item");
                var perType = new Dictionary<string, Dictionary<string, double>>();

                foreach (var tk in EnumerateAny(btT.GetProperty("Keys")?.GetValue(byType), 8))
                {
                    string typeName = Describe(tk);
                    var inner = getType?.Invoke(byType, new[] { tk });
                    if (inner == null) continue;

                    var inT = inner.GetType();
                    var getVal = inT.GetMethod("get_Item");
                    var bucket = new Dictionary<string, double>();
                    foreach (var ik in EnumerateAny(inT.GetProperty("Keys")?.GetValue(inner), 24))
                    {
                        string part = Describe(ik);
                        try
                        {
                            var v = getVal?.Invoke(inner, new[] { ik });
                            bucket[part] = Convert.ToDouble(Describe(v));
                        }
                        catch { }
                    }
                    if (bucket.Count > 0) perType[typeName] = bucket;
                }
                if (perType.Count > 0) pendingRoundStats[pid] = perType;
            }
        }
        catch (Exception e) { Log($"round stats error: {e.Message}"); }
    }

    private class TimelineEvent { public double T; public string Weapon; public int Receiver; public double Damage; }

    // turn the raw hit buffer into a clean round relative event list
    // AddDamageRecord fires N times per real hit so clusters are divided by N
    // N comes from comparing the raw sum to the roundRecords total for that weapon
    private static List<TimelineEvent> BuildRoundTimeline(double durationSec)
    {
        var outp = new List<TimelineEvent>();
        if (pendingRoundEvents.Count == 0 || durationSec <= 0) return outp;

        // receiver to weapon to true total
        var agg = new Dictionary<int, Dictionary<string, double>>();
        foreach (var pk in pendingRoundStats)
            if (pk.Value.TryGetValue("Damage", out var bucket))
                agg[pk.Key] = new Dictionary<string, double>(bucket);

        // Time.time when the round started
        double zero = pendingRoundEndRealtime - durationSec;

        foreach (var g in pendingRoundEvents.GroupBy(e => new { e.Receiver, e.Weapon }))
        {
            int receiver = g.Key.Receiver;
            string weapon = g.Key.Weapon;
            double rawSum = g.Sum(e => (double)e.Damage);
            double trueTotal = agg.TryGetValue(receiver, out var wm) && wm.TryGetValue(weapon, out var tt) ? tt : 0.0;

            // duplication factor for this receiver and weapon
            int factor = 1;
            if (trueTotal > 0.01) { factor = (int)Math.Round(rawSum / trueTotal); if (factor < 1) factor = 1; }

            // identical frame and damage clusters, keeps real same frame multi hits like shotgun pellets
            double emitted = 0.0;
            foreach (var cluster in g.GroupBy(e => new { F = Math.Round((double)e.TimeRaw, 3), D = Math.Round((double)e.Damage, 2) }))
            {
                int keep = (int)Math.Round(cluster.Count() / (double)factor);
                if (keep < 1) keep = 1;
                double t = Math.Max(0.0, Math.Min(durationSec, cluster.Key.F - zero));
                for (int i = 0; i < keep; i++)
                {
                    outp.Add(new TimelineEvent { T = Math.Round(t, 2), Weapon = weapon, Receiver = receiver, Damage = cluster.Key.D });
                    emitted += cluster.Key.D;
                }
            }

            // flag if the cleaned timeline doesnt add up to the aggregate
            if (trueTotal > 0.01 && Math.Abs(emitted - trueTotal) > 1.0)
                Log($"timeline off by {emitted - trueTotal:F1} for {weapon} on player {receiver}");
        }

        outp.Sort((a, b) => a.T.CompareTo(b.T));
        return outp;
    }

    // characters
    // health bar sprite name to character id, built once
    private static Dictionary<string, string> spriteToCharacter = null;

    // build the sprite to character map from the character settings
    public static void BuildCharacterMap()
    {
        if (spriteToCharacter != null || HealthHandlerInstance == null) return;
        try
        {
            var map = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            var cs = HealthHandlerInstance.GetType().GetProperty("characterSettings", F)?.GetValue(HealthHandlerInstance);
            if (cs == null) return;
            foreach (var it in EnumerateAny(cs.GetType().GetProperty("characterItems", F)?.GetValue(cs), 40))
            {
                if (it == null) continue;
                var itt = it.GetType();
                string cid = itt.GetProperty("characterId")?.GetValue(it)?.ToString();
                if (string.IsNullOrEmpty(cid)) continue;
                foreach (var sp in new[] { itt.GetProperty("healthBarIconPlayerOne")?.GetValue(it),
                                           itt.GetProperty("healthBarIconPlayerTwo")?.GetValue(it) })
                {
                    var n = sp?.GetType().GetProperty("name")?.GetValue(sp)?.ToString();
                    if (!string.IsNullOrEmpty(n)) map[n] = cid;
                }
            }
            if (map.Count > 0) spriteToCharacter = map;
        }
        catch (Exception e) { Log($"character map error: {e.Message}"); }
    }

    // character id for a sprite name or null
    public static string CharacterNameFor(string sprite)
    {
        if (string.IsNullOrEmpty(sprite) || spriteToCharacter == null) return null;
        return spriteToCharacter.TryGetValue(sprite, out var v) ? v : null;
    }

    // avatars
    // save each players portrait once and record which one they showed
    // bars are matched to players by the name printed on them, not by index
    private static void TryExportAvatars(Il2CppIgnis.AutoSpectateScoreboardReference scoreboard, List<PlayerRecord> players)
    {
        if (AvatarDir == null) return;

        try
        {
            var hbrs = scoreboard?.scoreBoardReference?.healthBarReferences;
            if (hbrs == null) return;
            for (int i = 0; i < hbrs.Length; i++)
            {
                var hb = hbrs[i];
                var img = hb?.characterIcon;
                var sp = img?.sprite;
                if (sp == null) continue;
                string spriteName = sp.name;
                if (string.IsNullOrEmpty(spriteName)) continue;

                string barName = null;
                try { barName = hb.text?.text; } catch { }

                var pr = string.IsNullOrEmpty(barName) ? null
                    : players.FirstOrDefault(p => string.Equals(barName, p.DisplayName, StringComparison.OrdinalIgnoreCase));

                if (pr == null) continue;
                pr.Character = spriteName;
                pr.CharacterName = CharacterNameFor(spriteName);
                // one file per distinct portrait ever
                string path = Path.Combine(AvatarDir, spriteName + ".png");
                if (!File.Exists(path))
                    ExportSpriteToPng(img, path);
            }
        }
        catch (Exception e) { Log($"avatar export error: {e.Message}"); }
    }

    // copy a sprite out of its non readable atlas into a png
    private static void ExportSpriteToPng(Image img, string path)
    {
        try
        {
            var sp = img?.sprite;
            var tex = sp?.texture;
            if (sp == null || tex == null) return;

            // sprite rect in texture space, bottom left origin
            var rect = sp.rect;
            int rx = Mathf.RoundToInt(rect.x), ry = Mathf.RoundToInt(rect.y);
            int rw = Mathf.RoundToInt(rect.width), rh = Mathf.RoundToInt(rect.height);
            if (rw <= 0 || rh <= 0) return;

            // blit the atlas to a temp render texture so the pixels can be read
            var rt = RenderTexture.GetTemporary(tex.width, tex.height, 0,
                RenderTextureFormat.ARGB32, RenderTextureReadWrite.sRGB);
            Graphics.Blit(tex, rt);
            var prev = RenderTexture.active;
            RenderTexture.active = rt;

            var readable = new Texture2D(rw, rh, TextureFormat.RGBA32, false);
            // if portraits come out flipped change ry to tex.height - ry - rh
            readable.ReadPixels(new Rect(rx, ry, rw, rh), 0, 0);
            readable.Apply();

            RenderTexture.active = prev;
            RenderTexture.ReleaseTemporary(rt);

            // il2cpp byte array to managed byte array
            var png = ImageConversion.EncodeToPNG(readable);
            int len = png.Length;
            var bytes = new byte[len];
            for (int k = 0; k < len; k++) bytes[k] = png[k];

            Directory.CreateDirectory(Path.GetDirectoryName(path));
            File.WriteAllBytes(path, bytes);
            UnityEngine.Object.Destroy(readable);

            Log($"saved new avatar: {sp.name}");
        }
        catch (Exception e) { Log($"avatar png error: {e.Message}"); }
    }

    // ping
    // add each players current ping to their running total, averaged at save
    private static void SamplePing(Il2CppIgnis.AutoSpectateScoreboardViewHandler handler, List<PlayerRecord> players)
    {
        try
        {
            var pingDict = handler.pingInformationData?.playerPingValues;
            if (pingDict == null) return;
            foreach (var key in pingDict.Keys)
            {
                int nid = (int)key;
                int val = (int)pingDict[key];
                if (val <= 0) continue;   // skip unset readings
                var pr = players.FirstOrDefault(p => p.NetworkId == nid);
                if (pr != null) { pr.PingSum += val; pr.PingSamples++; }
            }
        }
        catch (Exception e) { Log($"ping sample error: {e.Message}"); }
    }

    // harmony patches
    // runs at every round end, builds the match record and saves it when someone hits 2
    [HarmonyPatch]
    public class MatchEndPatch
    {
        static MethodBase TargetMethod()
        {
            var t = AccessTools.TypeByName("Il2CppIgnis.AutoSpectateScoreboardViewHandler");
            if (t == null) { Log("AutoSpectateScoreboardViewHandler not found, matches wont be recorded"); return null; }
            return AccessTools.Method(t, "SetWinMarkers");
        }

        // SetWinMarkers can fire in bursts so ignore calls within 500ms
        private static DateTime lastRoundTime = DateTime.MinValue;

        static void Postfix(object __instance)
        {
            if ((DateTime.UtcNow - lastRoundTime).TotalMilliseconds < 500) return;
            lastRoundTime = DateTime.UtcNow;

            try
            {
                // first round of a new match
                if (currentMatch == null)
                {
                    currentMatch = new MatchRecord { Timestamp = DateTime.UtcNow };
                    SpectatorCamera.ResetForNewMatch();
                }

                // rewrap the instance as the typed il2cpp handler
                var ptr = (__instance as Il2CppInterop.Runtime.InteropTypes.Il2CppObjectBase)?.Pointer;
                if (ptr == null) { Log("scoreboard handler pointer missing"); return; }
                var handler = new Il2CppIgnis.AutoSpectateScoreboardViewHandler(ptr.Value);

                var scoreboard = GetScoreboardReference();

                // character names once the roster is known
                if (currentMatch.Players.Count == 2)
                {
                    BuildCharacterMap();
                }

                // weapons shown on the scoreboard
                if (scoreboard != null && currentMatch.Players.Count == 2)
                {
                    var p1Weapons = new List<WeaponRecord>();
                    foreach (var icon in scoreboard.player1ToolIcons)
                    {
                        if (icon?.weaponName?.text == null) continue;
                        p1Weapons.Add(new WeaponRecord
                        {
                            Name = icon.weaponName.text,
                            Attachment = (icon.attachmentIconObject != null && icon.attachmentIconObject.activeSelf)
                                ? icon.attachmentImage?.activeSprite?.name ?? ""
                                : ""
                        });
                    }

                    var p2Weapons = new List<WeaponRecord>();
                    foreach (var icon in scoreboard.player2ToolIcons)
                    {
                        if (icon?.weaponName?.text == null) continue;
                        p2Weapons.Add(new WeaponRecord
                        {
                            Name = icon.weaponName.text,
                            Attachment = (icon.attachmentIconObject != null && icon.attachmentIconObject.activeSelf)
                                ? icon.attachmentImage?.activeSprite?.name ?? ""
                                : ""
                        });
                    }

                    currentMatch.Players[0].Weapons = p1Weapons;
                    currentMatch.Players[1].Weapons = p2Weapons;
                }

                // populate the roster on the first round
                if (currentMatch.Players.Count == 0)
                {
                    var gameData = handler.gameData;
                    if (gameData?.players != null)
                    {
                        foreach (var player in gameData.players)
                        {
                            if (player == null) continue;
                            string name = player.displayName ?? "Unknown";
                            if (string.IsNullOrEmpty(name) || name == "Unknown") continue;
                            currentMatch.Players.Add(new PlayerRecord
                            {
                                DisplayName = name,
                                LeaguePoints = player.leaguePoints,
                                NetworkId = player.playerNetworkId,
                                CharacterId = player.characterId,
                            });
                        }
                    }

                    if (currentMatch.Players.Count == 2)
                        TryExportAvatars(scoreboard, currentMatch.Players);
                }

                if (currentMatch.Players.Count == 2)
                    SamplePing(handler, currentMatch.Players);

                var mhd = handler.matchHandlerData;
                if (mhd == null) { Log("match handler data missing"); return; }

                int roundWinnerId = mhd.roundWinnerId;

                // no winner means someone disconnected so throw the match away
                if (roundWinnerId < 0)
                {
                    Log("player disconnected, match dropped");
                    roundDamageLatched = false;
                    roundStatsCaptured = false;
                    currentMatch = null;
                    pendingRoundEvents.Clear();
                    return;
                }

                var roundsWon = mhd.roundsWon;
                if (roundsWon != null)
                {
                    currentMatch.RoundsWon.Clear();
                    foreach (var key in roundsWon.Keys)
                        currentMatch.RoundsWon[(int)key] = (int)roundsWon[key];
                }

                // all zeros means the scoreboard is still setting up
                if (currentMatch.RoundsWon.Values.All(v => v == 0))
                {
                    return;
                }

                currentMatch.RoundWinnerIds.Add(roundWinnerId);
                currentMatch.RoundCount++;
                // point the camera at the round winner, their rig is guaranteed live
                SpectatorCamera.TargetNetworkId = roundWinnerId;

                // fight length from the round clock, excludes the countdown
                float startTs = mhd.startRoundTimeStamp;
                float endTs = mhd.endRoundTimestamp;
                double durationSec = (endTs > startTs) ? Math.Round((double)(endTs - startTs), 1) : 0.0;
                if (durationSec > 130.0) durationSec = 0.0;   // nonsense value

                bool forfeit = mhd.endingRoundThroughForfeit;

                // health left from the damage latched in CleanUpRound, loser overshoots so clamp at 0
                int? p1Health = null, p2Health = null;
                if (roundDamageLatched)
                {
                    p1Health = Math.Max(0, MAX_HEALTH - pendingDmg0);
                    p2Health = Math.Max(0, MAX_HEALTH - pendingDmg1);
                    roundDamageLatched = false;
                }

                currentMatch.Rounds.Add(new RoundSnapshot
                {
                    RoundNumber = currentMatch.RoundCount,
                    WinnerId = roundWinnerId,
                    DurationSeconds = durationSec,
                    EndedByForfeit = forfeit,
                    ForfeitPlayerId = mhd.forfeitPlayer,
                    P1Health = p1Health,
                    P2Health = p2Health,
                    Stats = pendingRoundStats.Count > 0
                        ? new Dictionary<int, Dictionary<string, Dictionary<string, double>>>(pendingRoundStats)
                        : null,
                    Events = BuildRoundTimeline(durationSec),
                });

                // reset for the next round
                pendingRoundStats.Clear();
                pendingRoundEvents.Clear();
                roundStatsCaptured = false;

                string roundWinner = currentMatch.Players.FirstOrDefault(p => p.NetworkId == roundWinnerId)?.DisplayName ?? $"player {roundWinnerId}";
                Log($"round {currentMatch.RoundCount}: {roundWinner} wins in {durationSec:F1}s{(forfeit ? " by forfeit" : "")}");

                // nobody on 2 yet so keep going
                if (!currentMatch.RoundsWon.Values.Any(v => v >= 2)) return;

                if (currentMatch.RoundCount < 2)
                {
                    Log("match too short, dropped");
                    currentMatch = null;
                    return;
                }

                currentMatch.EndReason = mhd.endMatchReason.ToString();
                currentMatch.MatchEndTime = DateTime.UtcNow;
                currentMatch.MatchId = PendingMatchId;   // lets api.py merge with the poller entry
                currentMatch.Region = PendingRegion;

                // average ping over the match
                foreach (var pr in currentMatch.Players)
                    if (pr.PingSamples > 0)
                        pr.Ping = (int)Math.Round((double)pr.PingSum / pr.PingSamples);

                // hand off and clear so nothing leaks into the next match
                var completed = currentMatch;
                currentMatch = null;
                PendingMatchId = null;
                PendingRegion = null;
                SaveMatchAsync(completed);
            }
            catch (Exception e)
            {
                Log($"match record error: {e}");
            }
        }
    }

    // grab round stats and end of round damage before CleanUpRound resets them
    // fires several times per round end, always before SetWinMarkers
    [HarmonyPatch]
    public class RoundDamageCapturePatch
    {
        static MethodBase TargetMethod()
        {
            var t = AccessTools.TypeByName("Il2CppIgnis.PlayerHealthHandler");
            if (t == null) { Log("PlayerHealthHandler not found, round health wont be recorded"); return null; }
            var m = AccessTools.Method(t, "CleanUpRound");
            if (m == null) Log("CleanUpRound not found, round health wont be recorded");
            return m;
        }

        static void Prefix(object __instance)
        {
            try
            {
                HealthHandlerInstance = __instance;

                // once per round end
                if (!roundStatsCaptured)
                {
                    CaptureRoundStats(__instance);
                    roundStatsCaptured = true;
                }

                if (roundDamageLatched) return;
                var dmg = ReadDamageTaken(__instance);
                if (dmg == null) return;
                pendingDmg0 = (int)Math.Round(dmg[0]);
                pendingDmg1 = (int)Math.Round(dmg[1]);
                pendingRoundEndRealtime = Time.time; // anchors the timeline
                roundDamageLatched = true;
            }
            catch (Exception e) { Log($"round damage error: {e.Message}"); }
        }
    }

    // buffer every damage hit for the round timeline
    // same passive tracking call that fills roundRecords, not a Tool hook
    [HarmonyPatch]
    public class DamageEventCapturePatch
    {
        static MethodBase TargetMethod()
        {
            var t = AccessTools.TypeByName("Il2CppIgnis.GameDesignTrackingData");
            if (t == null) { Log("GameDesignTrackingData not found, timeline wont be recorded"); return null; }
            var m = AccessTools.Method(t, "AddDamageRecord");
            if (m == null) Log("AddDamageRecord not found, timeline wont be recorded");
            return m;
        }

        // __0 type, __1 weapon, __2 damage, __3 receiver network id
        static void Prefix(object __0, string __1, float __2, int __3)
        {
            try
            {
                // damage only, grab and collision are bookkeeping noise
                var ty = Describe(__0);
                if (ty != "Damage" && ty != "0") return;
                if (__3 < 0) return; // spectator seat
                pendingRoundEvents.Add(new DamageEvent
                {
                    TimeRaw = Time.time,
                    Weapon = __1 ?? "",
                    Receiver = __3,
                    Damage = __2,
                });
            }
            catch (Exception e) { Log($"damage event error: {e.Message}"); }
        }
    }

    // saving
    private static readonly object saveLock = new object();

    // save off the main thread so a big matches.json cant hitch the game
    // safe because MatchRecord only holds plain managed data
    private static void SaveMatchAsync(MatchRecord match)
    {
        if (match == null || LogPath == null) return;
        System.Threading.Tasks.Task.Run((Action)(() =>
        {
            try { SaveMatch(match); }
            catch (Exception e) { Log($"save error: {e}"); }
        }));
    }

    // append one entry by trimming the closing ] and writing ,entry]
    // anything unexpected falls back to the full rewrite
    private static void AppendMatchEntry(string entry)
    {
        try
        {
            if (!File.Exists(LogPath) || new FileInfo(LogPath).Length == 0)
            {
                File.WriteAllText(LogPath, "[\n" + entry + "\n]");
                return;
            }

            using (var fs = new FileStream(LogPath, FileMode.Open, FileAccess.ReadWrite))
            {
                // last non whitespace byte should be ]
                long i = fs.Length - 1;
                int c = -1;
                while (i >= 0)
                {
                    fs.Position = i;
                    c = fs.ReadByte();
                    if (c != ' ' && c != '\t' && c != '\n' && c != '\r') break;
                    i--;
                }
                if (c != ']') { AppendMatchEntryFallback(entry); return; }

                // byte before it is [ for an empty array or } if it has entries
                long j = i - 1;
                int prev = -1;
                while (j >= 0)
                {
                    fs.Position = j;
                    prev = fs.ReadByte();
                    if (prev != ' ' && prev != '\t' && prev != '\n' && prev != '\r') break;
                    j--;
                }
                if (prev != (int)'[' && prev != (int)'}') { AppendMatchEntryFallback(entry); return; }

                bool empty = prev == (int)'[';
                fs.SetLength(j + 1);
                fs.Position = fs.Length;
                byte[] tail = Encoding.UTF8.GetBytes((empty ? "\n" : ",\n") + entry + "\n]");
                fs.Write(tail, 0, tail.Length);
            }
        }
        catch (Exception e)
        {
            Log($"fast append failed ({e.Message}), rewriting whole file");
            AppendMatchEntryFallback(entry);
        }
    }

    // slow but safe read and rewrite of the whole file
    private static void AppendMatchEntryFallback(string entry)
    {
        var entries = new List<string>();
        if (File.Exists(LogPath))
        {
            string existing = File.ReadAllText(LogPath).Trim();
            if (existing.StartsWith("[") && existing.EndsWith("]"))
            {
                string inner = existing.Substring(1, existing.Length - 2).Trim();
                if (!string.IsNullOrEmpty(inner)) entries.Add(inner);
            }
        }
        entries.Add(entry);
        File.WriteAllText(LogPath, "[\n" + string.Join(",\n", entries) + "\n]");
    }

    // serialise a finished match to json and append it
    private static void SaveMatch(MatchRecord match)
    {
        try
        {
            var actorNames = match.Players.ToDictionary(p => p.NetworkId, p => p.DisplayName);

            string winnerName = match.RoundsWon
                .OrderByDescending(kvp => kvp.Value)
                .Select(kvp => actorNames.TryGetValue(kvp.Key, out var n) ? n : $"actor_{kvp.Key}")
                .FirstOrDefault() ?? "Unknown";

            var inv = System.Globalization.CultureInfo.InvariantCulture;

            // match header
            var sb = new StringBuilder();
            sb.AppendLine("{");
            if (!string.IsNullOrEmpty(match.MatchId))
                sb.AppendLine($"  \"match_id\": \"{J(match.MatchId)}\",");
            if (!string.IsNullOrEmpty(match.Region))
                sb.AppendLine($"  \"region\": \"{J(match.Region)}\",");
            sb.AppendLine($"  \"timestamp\": \"{match.Timestamp:O}\",");
            sb.AppendLine($"  \"matchEndTime\": \"{match.MatchEndTime:O}\",");
            sb.AppendLine($"  \"endReason\": \"{J(match.EndReason)}\",");
            sb.AppendLine($"  \"winner\": \"{J(winnerName)}\",");
            sb.AppendLine($"  \"score\": \"{(match.RoundCount == 2 ? "2-0" : "2-1")}\",");
            sb.AppendLine($"  \"roundCount\": {match.RoundCount},");

            // players
            sb.AppendLine("  \"players\": [");
            for (int i = 0; i < match.Players.Count; i++)
            {
                var p = match.Players[i];
                string comma = i < match.Players.Count - 1 ? "," : "";
                var weaponsJson = string.Join(",", p.Weapons.Select(w => $"{{\"name\":\"{J(w.Name)}\",\"attachment\":\"{J(w.Attachment)}\"}}"));
                string pingJson = p.Ping >= 0 ? $", \"ping\": {p.Ping}" : "";
                string charJson = !string.IsNullOrEmpty(p.Character) ? $", \"character\": \"{J(p.Character)}\"" : "";
                if (!string.IsNullOrEmpty(p.CharacterName)) charJson += $", \"characterName\": \"{J(p.CharacterName)}\"";
                charJson += $", \"isHost\": {(p.NetworkId == 1 ? "true" : "false")}";
                sb.AppendLine($"    {{\"displayName\": \"{J(p.DisplayName)}\", \"leaguePoints\": {p.LeaguePoints}, \"characterId\": {p.CharacterId}{charJson}, \"networkId\": {p.NetworkId}{pingJson}, \"weapons\": [{weaponsJson}]}}{comma}");
            }
            sb.AppendLine("  ],");

            // rounds, optional fields only emit when populated
            sb.AppendLine("  \"rounds\": [");
            for (int i = 0; i < match.Rounds.Count; i++)
            {
                var r = match.Rounds[i];
                string comma = i < match.Rounds.Count - 1 ? "," : "";
                string rWinner = actorNames.TryGetValue(r.WinnerId, out var rn) ? rn : $"actor_{r.WinnerId}";
                var parts = new List<string>
                {
                    $"\"round\": {r.RoundNumber}",
                    $"\"winner\": \"{J(rWinner)}\""
                };
                if (r.DurationSeconds > 0) parts.Add($"\"durationSeconds\": {r.DurationSeconds.ToString(inv)}");
                if (r.EndedByForfeit)
                {
                    parts.Add("\"endedByForfeit\": true");
                    parts.Add($"\"forfeitPlayerId\": {r.ForfeitPlayerId}");
                }
                if (r.P1Health.HasValue) parts.Add($"\"p1Health\": {r.P1Health.Value.ToString(inv)}");
                if (r.P2Health.HasValue) parts.Add($"\"p2Health\": {r.P2Health.Value.ToString(inv)}");

                // stats as {"1":{"Damage":{"Bow":45.2},"Grab":{...}}, "2":{...}}
                if (r.Stats != null && r.Stats.Count > 0)
                {
                    var players = r.Stats.Select(pk =>
                    {
                        var types = pk.Value.Select(tk =>
                        {
                            var statEntries = tk.Value.Select(e => $"\"{J(e.Key)}\": {Math.Round(e.Value, 1).ToString(inv)}");
                            return $"\"{J(tk.Key)}\": {{{string.Join(", ", statEntries)}}}";
                        });
                        return $"\"{pk.Key}\": {{{string.Join(", ", types)}}}";
                    });
                    parts.Add($"\"stats\": {{{string.Join(", ", players)}}}");
                }

                // per hit timeline
                if (r.Events != null && r.Events.Count > 0)
                {
                    var evs = r.Events.Select(e =>
                        $"{{\"t\": {e.T.ToString(inv)}, \"weapon\": \"{J(e.Weapon)}\", \"receiver\": {e.Receiver}, \"dmg\": {e.Damage.ToString(inv)}}}");
                    parts.Add($"\"events\": [{string.Join(", ", evs)}]");
                }
                sb.AppendLine($"    {{{string.Join(", ", parts)}}}{comma}");
            }
            sb.AppendLine("  ],");

            // round winners by name
            sb.AppendLine("  \"roundWinners\": [");
            for (int i = 0; i < match.RoundWinnerIds.Count; i++)
            {
                int id = match.RoundWinnerIds[i];
                string name = actorNames.TryGetValue(id, out var n) ? n : $"actor_{id}";
                string comma = i < match.RoundWinnerIds.Count - 1 ? "," : "";
                sb.AppendLine($"    \"{J(name)}\"{comma}");
            }
            sb.AppendLine("  ]");
            sb.AppendLine("}");

            lock (saveLock)
                AppendMatchEntry(sb.ToString().Trim());

            Log($"match saved: {winnerName} wins {(match.RoundCount == 2 ? "2-0" : "2-1")}");
        }
        catch (Exception e)
        {
            Log($"save match error: {e}");
        }
    }

    // records
    private class MatchRecord
    {
        public string MatchId;
        public string Region;
        public DateTime Timestamp;
        public DateTime MatchEndTime;
        public string EndReason;
        public List<PlayerRecord> Players = new List<PlayerRecord>();
        public List<int> RoundWinnerIds = new List<int>();
        public Dictionary<int, int> RoundsWon = new Dictionary<int, int>();
        public List<RoundSnapshot> Rounds = new List<RoundSnapshot>();
        public int RoundCount;
    }

    private class RoundSnapshot
    {
        public int RoundNumber;
        public int WinnerId;
        public double DurationSeconds;
        public bool EndedByForfeit;
        public int ForfeitPlayerId;
        public int? P1Health;
        public int? P2Health;
        public Dictionary<int, Dictionary<string, Dictionary<string, double>>> Stats;
        public List<TimelineEvent> Events;
    }

    private class WeaponRecord
    {
        public string Name;
        public string Attachment;
    }

    private class PlayerRecord
    {
        public string DisplayName;
        public int LeaguePoints;
        public int NetworkId;
        public int CharacterId;
        public string Character; // portrait sprite name
        public string CharacterName; // resolved character id
        public int Ping = -1; // averaged ms, -1 is unknown
        public long PingSum = 0;
        public int PingSamples = 0;
        public List<WeaponRecord> Weapons = new List<WeaponRecord>();
    }
}

// spectator camera
// re point the spectator camera every tick until it is following the target player
[HarmonyPatch]
public class SpectatorCameraTickPatch
{
    static MethodBase TargetMethod()
    {
        var t = AccessTools.TypeByName("Il2CppIgnis.SpectatorCameraHandler");
        return t == null ? null : AccessTools.Method(t, "Tick");
    }

    static void Prefix(object __instance) => SpectatorCamera.OnTick(__instance);

    // the rig isnt spawned for a few frames after load so Tick throws, swallow just that
    static Exception Finalizer(Exception __exception)
        => __exception is NullReferenceException ? null : __exception;
}

public static class SpectatorCamera
{
    public static int TargetNetworkId = 1; // round 1 starts on player 1, then follows round winners

    const BindingFlags F = BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance;

    static bool _ready, _initLogged;
    static int _loggedTarget = -1; // last target we logged landing on
    static string _lastError; // stops a repeating error spamming every frame
    static PropertyInfo _dataP, _gdP, _followedP, _modeP, _seatP, _netIdP, _viewsP, _inputP, _tp1P, _tp2P, _ccmP;
    static FieldInfo _dataF, _gdF, _followedF, _modeF, _seatF, _viewsF, _inputF, _tp1F, _tp2F, _ccmF;
    static PropertyInfo _playerRefP, _tpPointP, _posP;

    // members can be a property or a field so resolve whichever exists
    static void Resolve(Type t, string name, out PropertyInfo p, out FieldInfo fi)
    {
        p = t.GetProperty(name, F);
        fi = p == null ? t.GetField(name, F) : null;
    }
    static object Get(object o, PropertyInfo p, FieldInfo fi) => p != null ? p.GetValue(o) : fi?.GetValue(o);
    static void Set(object o, PropertyInfo p, FieldInfo fi, object v) { if (p != null) p.SetValue(o, v); else fi?.SetValue(o, v); }

    // resolve the camera handler and data members once
    static bool Init()
    {
        if (_ready) return true;
        var hT = AccessTools.TypeByName("Il2CppIgnis.SpectatorCameraHandler");
        var dT = AccessTools.TypeByName("Il2CppIgnis.SpectatorCameraData");
        if (hT == null || dT == null) return false;

        Resolve(hT, "spectatorCameraData", out _dataP, out _dataF);
        Resolve(hT, "gameData", out _gdP, out _gdF);
        Resolve(hT, "currentlyFollowedPlayer", out _followedP, out _followedF);
        Resolve(hT, "inputData", out _inputP, out _inputF);
        Resolve(dT, "cameraModeType", out _modeP, out _modeF);
        Resolve(dT, "currentSeatType", out _seatP, out _seatF);
        Resolve(dT, "spectatorViews", out _viewsP, out _viewsF);

        bool have(PropertyInfo p, FieldInfo fi) => p != null || fi != null;
        _ready = have(_dataP, _dataF) && have(_gdP, _gdF) && have(_followedP, _followedF)
              && have(_modeP, _modeF) && have(_seatP, _seatF);
        if (!_ready && !_initLogged)
        {
            _initLogged = true;
            BlastonTracker.Log("spectator camera members missing, camera control disabled");
        }
        return _ready;
    }

    // true once a players third person camera anchor can be read without throwing
    static bool PlayerRigLive(object player)
    {
        try
        {
            if (_playerRefP == null) _playerRefP = player.GetType().GetProperty("playerReference", F);
            if (_playerRefP == null) return false;

            var pref = _playerRefP.GetValue(player);
            if (pref == null) return false; // rig not live

            // the anchor Tick reads get_position from
            if (_tpPointP == null) _tpPointP = pref.GetType().GetProperty("thirdPersonCameraPointRightShoulder", F);
            if (_tpPointP == null) return false;

            var tf = _tpPointP.GetValue(pref);
            if (tf == null) return false;

            if (_posP == null) _posP = tf.GetType().GetProperty("position", F);
            return _posP?.GetValue(tf) != null;
        }
        catch { return false; }
    }

    // network id of a PlayerData or -1
    static int NetworkIdOf(object player)
    {
        try
        {
            if (player == null) return -1;
            if (_netIdP == null) _netIdP = player.GetType().GetProperty("playerNetworkId", F);
            return _netIdP == null ? -1 : Convert.ToInt32(_netIdP.GetValue(player));
        }
        catch { return -1; }
    }

    // find the PlayerData with a given network id in the handlers game data
    static object FindPlayer(object handler, int nid)
    {
        var gd = Get(handler, _gdP, _gdF);
        var players = gd?.GetType().GetProperty("players", F)?.GetValue(gd);
        if (players == null) return null;
        var lt = players.GetType();
        int n = (int)(lt.GetProperty("Count")?.GetValue(players) ?? 0);
        var get = lt.GetMethod("get_Item", new[] { typeof(int) });
        for (int i = 0; i < n; i++)
        {
            var p = get.Invoke(players, new object[] { i });
            if (NetworkIdOf(p) == nid) return p;
        }
        return null;
    }

    // called every camera tick, nudges the game input until the camera follows the target
    public static void OnTick(object handler)
    {
        if (!Init()) return;
        try
        {
            var data = Get(handler, _dataP, _dataF);
            if (data == null) return;

            var followedPlayer = Get(handler, _followedP, _followedF);
            int followed = NetworkIdOf(followedPlayer);
            int seat = Convert.ToInt32(Get(data, _seatP, _seatF));

            // already on the target, but only if their rig is live
            // the handler keeps last matchs player which has the same id but a dead rig
            if (seat == 1 && followed == TargetNetworkId
                && followedPlayer != null && PlayerRigLive(followedPlayer))
            {
                // log once per landing, not once per frame
                if (_loggedTarget != TargetNetworkId)
                {
                    _loggedTarget = TargetNetworkId;
                    BlastonTracker.Log($"camera following player {TargetNetworkId}");
                }
                return;
            }

            var player = FindPlayer(handler, TargetNetworkId);
            if (player == null) return;
            if (Get(data, _viewsP, _viewsF) == null) return;
            if (!PlayerRigLive(player)) return;

            var input = Get(handler, _inputP, _inputF);
            if (input == null) return;
            if (_tp1P == null && _tp1F == null) Resolve(input.GetType(), "thirdPersonPlayer1", out _tp1P, out _tp1F);
            if (_tp2P == null && _tp2F == null) Resolve(input.GetType(), "thirdPersonPlayer2", out _tp2P, out _tp2F);
            if (_ccmP == null && _ccmF == null) Resolve(input.GetType(), "changeCameraMode", out _ccmP, out _ccmF);

            // still in auto so press change mode first, manual is 1
            int mode = Convert.ToInt32(Get(data, _modeP, _modeF));
            if (mode != 1)
            {
                Set(input, _ccmP, _ccmF, true);
                return;
            }

            // in manual so keep pressing the third person key for the target
            if (TargetNetworkId == 1) Set(input, _tp1P, _tp1F, true);
            else Set(input, _tp2P, _tp2F, true);

        }
        catch (Exception e)
        {
            if (e.Message != _lastError)
            {
                _lastError = e.Message;
                BlastonTracker.Log($"camera error: {e.Message}");
            }
        }
    }

    // new match always starts on player 1
    public static void ResetForNewMatch() { TargetNetworkId = 1; _loggedTarget = -1; }
}