using System;
using UnityEngine;
using UnityEngine.InputSystem;
using UnityEngine.InputSystem.LowLevel;

namespace DD2TextMod
{
    /// <summary>
    /// 进程内虚拟手柄：用 Unity 新版 Input System 插一个"假手柄"，推左摇杆。
    /// 对游戏来说这就是有人真的在用手柄，跟窗口焦点、系统光标都没关系。
    /// </summary>
    internal static class VirtualPad
    {
        private static Gamepad _pad;

        public static Gamepad Pad
        {
            get
            {
                if (_pad == null || !_pad.added)
                {
                    try
                    {
                        _pad = InputSystem.AddDevice<Gamepad>("DD2VirtualPad");
                        Plugin.Log?.LogInfo("virtual gamepad added: " + (_pad != null ? _pad.deviceId.ToString() : "null"));
                    }
                    catch (Exception e)
                    {
                        Plugin.Log?.LogError("add gamepad failed: " + e.Message);
                        return null;
                    }
                }
                return _pad;
            }
        }

        /// <summary>推左摇杆：x 左右，y 前后（1=推到底前进）。</summary>
        public static object Push(float x, float y, string buttons)
        {
            var pad = Pad;
            if (pad == null) return new { ok = false, error = "virtual gamepad not available" };

            var state = new GamepadState { leftStick = new Vector2(Mathf.Clamp(x, -1f, 1f), Mathf.Clamp(y, -1f, 1f)) };
            if (!string.IsNullOrEmpty(buttons))
            {
                foreach (var b in buttons.ToLower().Split(','))
                {
                    switch (b.Trim())
                    {
                        case "south": state = state.WithButton(GamepadButton.South, true); break;
                        case "east": state = state.WithButton(GamepadButton.East, true); break;
                        case "west": state = state.WithButton(GamepadButton.West, true); break;
                        case "north": state = state.WithButton(GamepadButton.North, true); break;
                        case "start": state = state.WithButton(GamepadButton.Start, true); break;
                        case "dpadup": state = state.WithButton(GamepadButton.DpadUp, true); break;
                        case "dpaddown": state = state.WithButton(GamepadButton.DpadDown, true); break;
                        case "dpadleft": state = state.WithButton(GamepadButton.DpadLeft, true); break;
                        case "dpadright": state = state.WithButton(GamepadButton.DpadRight, true); break;
                        case "leftshoulder": state = state.WithButton(GamepadButton.LeftShoulder, true); break;
                        case "rightshoulder": state = state.WithButton(GamepadButton.RightShoulder, true); break;
                        case "lefttrigger": state = state.WithButton(GamepadButton.LeftTrigger, true); break;
                        case "righttrigger": state = state.WithButton(GamepadButton.RightTrigger, true); break;
                    }
                }
            }
            InputSystem.QueueStateEvent(pad, state);
            return new { ok = true, device = pad.deviceId, x, y, buttons, enabled = pad.enabled, added = pad.added };
        }

        public static object Release()
        {
            var pad = Pad;
            if (pad == null) return new { ok = false, error = "no pad" };
            InputSystem.QueueStateEvent(pad, new GamepadState());
            return new { ok = true, released = true };
        }

        public static object Info()
        {
            var pad = Pad;
            if (pad == null) return new { ok = false, error = "no pad" };
            return new
            {
                ok = true,
                deviceId = pad.deviceId,
                name = pad.name,
                displayName = pad.displayName,
                added = pad.added,
                enabled = pad.enabled,
                layout = pad.layout,
                stick = pad.leftStick.ReadValue().ToString(),
                currentDevice = InputSystem.GetDevice<Gamepad>() != null,
            };
        }
    }
}
