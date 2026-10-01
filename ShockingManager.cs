/*
 * ShockingVRC
 * VRC群组: https://vrc.group/SHOCK.2911
 * QQ群: 948779199
 * VRC 郊狼OSC联动插件: https://github.com/VRChatNext/Shocking-VRChat
 * 联动插件作者: 亨亨0v0
 * Udon脚本作者: Ero小玉 /  Cheese
 * 对开发有疑惑可以联系小玉，QQ: 3690985317, cheese:2190038793
 * 触发郊狼需要联动插件最低版本: 0.5.0
 * 郊狼状态检测需要联动插件最低版本: 0.5.1
 * 小玉最后更新日期: 2024/11/17
 * cheese最后更新日记 2026/5/21
 */

using DeanCode;
using TMPro;
using UdonSharp;
using UnityEngine;
using VRC.SDK3.Data;
using VRC.SDK3.Image;
using VRC.SDK3.Persistence;
using VRC.SDK3.StringLoading;
using VRC.SDK3.UdonNetworkCalling;
using VRC.SDKBase;
using VRC.Udon;

/// <summary>
/// 统一从此处触发郊狼，方便管理CD与调度
/// </summary>
public class ShockingManager : UdonSharpBehaviour
{
    public UdonBehaviour UdonBehaviour;
    [Header("是否使用兼容老版本插件的Url检测")]
    public bool IsUsingUrl = true;
    [Space(10)]

    [Header("是否启用手柄震动")]
    public bool EnableHaptic = true;
    [Space(10)]

    public VRCUrl[] Urls;
    public VRCUrl StatusUrl; // 用于获取郊狼设备状态的url
    public bool DebugMode = false;
    private bool shockingFlag;

    [Header("ShockAll 扣钱全图电击")]
    [Tooltip("全图电击的价格（从 UdonChips 所持金中扣除）")]
    public int shockAllPrice = 1000;
    [Tooltip("全图电击的持续秒数（1-9，超过范围会自动钳制）")]
    public int shockAllSeconds = 3;
    [Tooltip("两次全图电击的最小间隔秒数，防止连点刷网络事件")]
    public float shockAllCooldown = 5f;
    [Tooltip("扣钱用的 UdonChips（必填，不填则全图电击不可用）")]
    public UCS.UdonChips udonChips;
    [Tooltip("发动全图电击时本地播放的音源（可选）")]
    public AudioSource shockAllAudioSource;
    [Tooltip("发动全图电击时播放的音效（可选，同时会作为通知的淡入音效）")]
    public AudioClip shockAllAudioClip;
    [Tooltip("通知栏管理器（可选），发动全图电击时所有人弹出\"XXX 发动了全员电击\"通知")]
    public NotificationManager notificationManager;
    [Tooltip("全图电击公告文本（可选），会显示\"XXX 发动了全员电击\"，5秒后自动清空")]
    public TextMeshProUGUI shockAllMessageTMP;
    private float lastShockAllTime = -999f;

    [Header("排行榜 UI（可选）：谁开了插件/连了郊狼")]
    [Tooltip("玩家名列（不接则整个排行榜功能关闭）")]
    public TextMeshProUGUI rankNamesTMP;
    [Tooltip("插件状态列（开/关）")]
    public TextMeshProUGUI rankPluginTMP;
    [Tooltip("郊狼连接列（开/关）")]
    public TextMeshProUGUI rankDGLabTMP;
    [Tooltip("A/B通道强度列（可选）")]
    public TextMeshProUGUI rankStrengthTMP;
    private float statusRefreshTimer;
    private const string KEY_PLUGIN = "IsConnectedShockingVRCPlugin";
    private const string KEY_DGLAB = "IsConnectedDGLab";
    private const string KEY_A = "AChannelStrength";
    private const string KEY_B = "BChannelStrength";

    #region 郊狼状态

    /// <summary>
    /// 是否连接到郊狼联动插件
    /// </summary>
    public bool IsConnectedShockingVRCPlugin { get; private set; }

    /// <summary>
    /// 联动插件是否连接到郊狼设备
    /// </summary>
    public bool IsConnectedDGLab { get; private set; }

    public int AChannelStrength { get; private set; }
    public int BChannelStrength { get; private set; }

    #endregion

    void Start()
    {
        // 进房 5 秒后检测一次郊狼状态，结果写入 PlayerData 自动同步全房间（排行榜数据源）
        SendCustomEventDelayedSeconds("RefreshLocalPlayerShockStatus", 5f);
    }

    private void Update()
    {
        // 每 180 秒重检一次自己的郊狼状态，掉线/上线能反映到排行榜
        statusRefreshTimer += Time.deltaTime;
        if (statusRefreshTimer > 180f)
        {
            statusRefreshTimer = 0f;
            RefreshLocalPlayerShockStatus();
        }
    }

    /*
    public override void OnPlayerJoined(VRCPlayerApi player)
    {
        base.OnPlayerJoined(player);
        if (player.isLocal)
        {
            RefreshLocalPlayerShockStatus();
        }
    }
    */

    private void Log(string message)
    {
        if (DebugMode)
        {
            Debug.Log(message);
        }
    }

    /// <summary>
    /// 简易触发，调用此方法会触发郊狼的AB通道3秒（无参数版本，供外部SendCustomEvent调用）
    /// </summary>
    public void JustShock1()
    {
        JustShock(VRC_Pickup.PickupHand.Right);
    }

    /// <summary>
    /// 简易触发，调用此方法会触发郊狼的AB通道3秒
    /// </summary>
    public void JustShock(VRC_Pickup.PickupHand hand = VRC_Pickup.PickupHand.Right)
    {
        LocalShocking(2, 3, hand);
    }

    #region 扣钱全图电击

    /// <summary>
    /// 扣钱后全图电击（参考 AfterLife 版 ShockAll 移植）：
    /// 扣掉自己 UdonChips 的所持金，然后广播网络事件，
    /// 让房间里所有人（含自己）在各自客户端触发郊狼 AB 通道 3 秒。
    /// 绑定到 UI Button 的 OnClick 即可。
    /// </summary>
    public void ShockAll()
    {
        if (udonChips == null)
        {
            Debug.LogError("[ShockingManager]未指定 UdonChips，无法扣钱发动全图电击");
            return;
        }

        // 冷却防连点
        if (Time.time - lastShockAllTime < shockAllCooldown)
        {
            Log("[ShockingManager]全图电击冷却中");
            return;
        }

        // 余额不足
        if (udonChips.money < shockAllPrice)
        {
            Log("[ShockingManager]金币不足，无法发动全图电击");
            return;
        }

        lastShockAllTime = Time.time;

        // 扣钱（UdonChips 会在自己的 Update 里检测到金额变化并触发存档/显示刷新）
        udonChips.money -= shockAllPrice;

        // 本地音效
        if (shockAllAudioSource != null && shockAllAudioClip != null)
        {
            shockAllAudioSource.PlayOneShot(shockAllAudioClip);
        }

        // 所有人（含自己）本地触发郊狼 AB 通道，时长由 shockAllSeconds 控制（1-9秒）
        SendCustomNetworkEvent(VRC.Udon.Common.Interfaces.NetworkEventTarget.All, "ShockAllShock", shockAllSeconds);
        // 广播公告（带参数网络事件，需要 NetworkCallable 标记）
        SendCustomNetworkEvent(VRC.Udon.Common.Interfaces.NetworkEventTarget.All, "shockAllMessage", Networking.LocalPlayer.displayName);

        Log($"[ShockingManager]发动全图电击，扣除 {shockAllPrice} 金币，电击 {shockAllSeconds} 秒");
    }

    /// <summary>
    /// 全图电击执行入口（网络事件，带秒数参数，不要改名）：
    /// 每个客户端本地触发郊狼 AB 通道电击
    /// </summary>
    [NetworkCallable]
    public void ShockAllShock(int seconds)
    {
        if (seconds < 1) seconds = 1;
        if (seconds > 9) seconds = 9;
        LocalShocking(2, seconds, VRC_Pickup.PickupHand.Right);
    }

    /// <summary>
    /// 全图电击公告（网络事件，带参调用需要 NetworkCallable）
    /// </summary>
    [NetworkCallable]
    public void shockAllMessage(string name)
    {
        Debug.Log("[ShockingManager]" + name + " 发动了全员电击");
        // 通知栏公告（与参考版同款）
        if (notificationManager != null)
        {
            notificationManager._SendNotification(name + " 发动了全员电击", NotificationType.Warning, shockAllAudioClip, null, 5f);
        }
        // TMP 文本公告（可选兜底）
        if (shockAllMessageTMP != null)
        {
            shockAllMessageTMP.text = name + " 发动了全员电击";
            SendCustomEventDelayedSeconds("HideShockAllMessage", 5f);
        }
    }

    /// <summary>
    /// 清空全图电击公告文本
    /// </summary>
    public void HideShockAllMessage()
    {
        if (shockAllMessageTMP != null)
        {
            shockAllMessageTMP.text = "";
        }
    }

    #endregion

    /// <summary>
    /// 本地触发郊狼
    /// 由于VRC的限制，最快只能2.5秒触发一次(原理是使用url请求触发，vrc对图片请求和字符串请求各有5秒钟的cd)
    /// 如果快速调用2次，可以一起触发，但需要等待5秒后再次调用
    /// </summary>
    /// <param name="mode">默认 0:A通道 1:B通道 2:AB通道</param>
    /// <param name="seconds">触发秒数，1-9秒整数</param>
    /// <param name="hand">触发手柄</param>
    public void LocalShocking(int mode, int seconds, VRC_Pickup.PickupHand hand = VRC_Pickup.PickupHand.Right)
    {
        //这是新版的触发方法,不要注释掉
        Debug.Log($"[DGLABCheeseShocking]LocalShocking调用: mode={mode}, seconds={seconds}, hand={hand}");

        if (isWaitingForStatus)
        {
            Log("[ShockingManager]正在等待检测状态的返回信息，请等待检测完成再调用电击");
            return;
        }

        if (seconds > 0 && seconds < 10)
        {
            if (mode >= 0 && mode < 3)
            {
                Shocking(Urls[seconds + mode * 10]);
                if (EnableHaptic)
                {
                    Networking.LocalPlayer.PlayHapticEventInHand(hand, seconds, 10, 15);
                }   
            }
            else
            {
                Debug.LogError("[ShockingManager]无效的模式: " + mode);
            }
        }
        else
        {
            Debug.LogError("[ShockingManager]无效的秒数: " + seconds);
        }
    }

    private void Shocking(VRCUrl url)
    {
        shockingFlag = !shockingFlag;
        if (shockingFlag)
        {
            VRCStringDownloader.LoadUrl(url, UdonBehaviour);
            Log($"[ShockingManager]通过字符串请求 {url}");
        }
        else
        {
            VRCImageDownloader imageDownloader = new VRCImageDownloader();
            imageDownloader.DownloadImage(url, null, UdonBehaviour);
            Log($"[ShockingManager]通过图片请求 {url}");
        }
    }

    public override void OnStringLoadSuccess(IVRCStringDownload result)
    {
        base.OnStringLoadSuccess(result);
        Log("[ShockingManager]接收了字符串: " + result.Result);
        if (isWaitingForStatus)
        {
            OnStatusLoadSuccess(result);
        }
    }

    public override void OnStringLoadError(IVRCStringDownload result)
    {
        base.OnStringLoadError(result);
        Log("[ShockingManager]字符串接收失败: " + result.Error);
        if (isWaitingForStatus)
        {
            OnStatusLoadError();
        }
    }

    public override void OnImageLoadSuccess(IVRCImageDownload result)
    {
        base.OnImageLoadSuccess(result);
        Log("[ShockingManager]接收了图片: " + result.Result);
    }

    public override void OnImageLoadError(IVRCImageDownload result)
    {
        base.OnImageLoadError(result);
        Log("[ShockingManager]图片接收失败: " + result.Error);
    }

    #region 检测郊狼状态

/*
 检测状态的返回值示例
{
    "devices": [
        {
            "attr": {
                "strength": {
                    "A": 5,
                    "B": 4
                },
                "uuid": "de3371e0-a09a-4e01-a942-986726f5afa7"
            },
            "device": "coyotev3",
            "type": "shock"
        }
    ],
    "healthy": "ok"
}
*/

    private bool isWaitingForStatus;

    /// <summary>
    /// 刷新本地玩家的郊狼状态，在需要时进行手动调用
    /// </summary>
    public void RefreshLocalPlayerShockStatus()
    {
        isWaitingForStatus = true;
        Log("[ShockingManager]开始检测郊狼状态");
        VRCStringDownloader.LoadUrl(StatusUrl, UdonBehaviour);
    }

    private void OnStatusLoadSuccess(IVRCStringDownload result)
    {
        isWaitingForStatus = false;
        IsConnectedShockingVRCPlugin = false;
        IsConnectedDGLab = false;
        if (VRCJson.TryDeserializeFromJson(result.Result, out DataToken token))
        {
            if (token.DataDictionary != null)
            {
                var dict = token.DataDictionary;
                if (dict.TryGetValue("devices", out DataToken devicesToken))
                {
                    if (devicesToken.DataList != null)
                    {
                        IsConnectedShockingVRCPlugin = true;
                        if (devicesToken.DataList.Count > 0)
                        {
                            var deviceData = devicesToken.DataList[0];
                            if (deviceData.DataDictionary.TryGetValue("attr", out DataToken attrToken))
                            {
                                if (attrToken.DataDictionary.TryGetValue("strength", out DataToken strengthToken))
                                {
                                    if (strengthToken.DataDictionary.TryGetValue("A", out DataToken aToken))
                                    {
                                        if (aToken.IsNumber)
                                        {
                                            AChannelStrength = (int)aToken.Double;
                                            IsConnectedDGLab = true;
                                        }
                                    }

                                    if (strengthToken.DataDictionary.TryGetValue("B", out DataToken bToken))
                                    {
                                        if (bToken.IsNumber)
                                        {
                                            BChannelStrength = (int)bToken.Double;
                                            IsConnectedDGLab = true;
                                        }
                                    }
                                }
                            }
                        }
                        else
                        {
                            AChannelStrength = 0;
                            BChannelStrength = 0;
                        }
                    }
                }
            }
        }

        // 状态写入 PlayerData（自动同步给全房间所有人，排行榜数据源）
        PlayerData.SetBool(KEY_PLUGIN, IsConnectedShockingVRCPlugin);
        PlayerData.SetBool(KEY_DGLAB, IsConnectedDGLab);
        PlayerData.SetInt(KEY_A, Mathf.Clamp(AChannelStrength, 0, 200));
        PlayerData.SetInt(KEY_B, Mathf.Clamp(BChannelStrength, 0, 200));

        LogStatus();
    }

    private void OnStatusLoadError()
    {
        isWaitingForStatus = false;
        IsConnectedShockingVRCPlugin = false;
        IsConnectedDGLab = false;
        AChannelStrength = 0;
        BChannelStrength = 0;
        // 检测失败也同步为离线状态
        PlayerData.SetBool(KEY_PLUGIN, false);
        PlayerData.SetBool(KEY_DGLAB, false);
        PlayerData.SetInt(KEY_A, 0);
        PlayerData.SetInt(KEY_B, 0);
        LogStatus();
    }

    private void LogStatus()
    {
        Debug.Log(
            $"[ShockingManager]是否连接到郊狼联动插件: {IsConnectedShockingVRCPlugin} 是否连接到郊狼设备: {IsConnectedDGLab} A通道强度: {AChannelStrength} B通道强度: {BChannelStrength}");
    }
    #endregion

    #region 排行榜（谁开了插件/连了郊狼）

    public override void OnPlayerJoined(VRCPlayerApi player)
    {
        RefreshRankUI();
    }

    public override void OnPlayerLeft(VRCPlayerApi player)
    {
        RefreshRankUI();
    }

    /// <summary>
    /// 任何人 PlayerData 变化时自动触发（自己写状态、别人同步、UdonChips 自动存档等）
    /// </summary>
    public override void OnPlayerDataUpdated(VRCPlayerApi player, PlayerData.Info[] infos)
    {
        RefreshRankUI();
    }

    /// <summary>
    /// 重建排行榜 UI：
    /// 数据来源是每个玩家客户端自己写入 PlayerData 的郊狼状态，
    /// 这里读取全房间玩家的数据并按行刷新到各列 TMP。
    /// 已连接郊狼的玩家排在前面。
    /// </summary>
    public void RefreshRankUI()
    {
        if (rankNamesTMP == null) return; // 没接 UI 直接跳过

        VRCPlayerApi[] all = new VRCPlayerApi[VRCPlayerApi.GetPlayerCount()];
        VRCPlayerApi.GetPlayers(all);

        string names = "";
        string pluginCol = "";
        string dgblabCol = "";
        string strengthCol = "";

        // 两遍扫描：第一遍取"插件+郊狼都开"的，第二遍取其他人，连接的人排前面
        for (int pass = 0; pass < 2; pass++)
        {
            for (int i = 0; i < all.Length; i++)
            {
                VRCPlayerApi p = all[i];
                if (p == null) continue;

                bool csp = PlayerData.HasKey(p, KEY_PLUGIN) && PlayerData.GetBool(p, KEY_PLUGIN);
                bool cdg = PlayerData.HasKey(p, KEY_DGLAB) && PlayerData.GetBool(p, KEY_DGLAB);
                bool open = csp && cdg;
                if (pass == 0 && !open) continue;
                if (pass == 1 && open) continue;

                int a = PlayerData.HasKey(p, KEY_A) ? PlayerData.GetInt(p, KEY_A) : 0;
                int b = PlayerData.HasKey(p, KEY_B) ? PlayerData.GetInt(p, KEY_B) : 0;
                if (!open)
                {
                    a = 0;
                    b = 0;
                }

                string n = p.displayName;
                n = n.Replace(" ", "");
                if (n.Length > 12) n = n.Substring(0, 12);

                names += n + "\n";
                pluginCol += (csp ? "<color=#0BFF23>开</color>" : "<color=#FF0000>关</color>") + "\n";
                dgblabCol += (cdg ? "<color=#0BFF23>开</color>" : "<color=#FF0000>关</color>") + "\n";
                strengthCol += a + "/" + b + "\n";
            }
        }

        rankNamesTMP.text = names;
        rankPluginTMP.text = pluginCol;
        rankDGLabTMP.text = dgblabCol;
        if (rankStrengthTMP != null) rankStrengthTMP.text = strengthCol;
        Log("[ShockingManager]排行榜已刷新");
    }

    #endregion
}
