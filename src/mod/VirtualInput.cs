using System;
using UnityEngine;
using UnityEngine.InputSystem;
using UnityEngine.InputSystem.LowLevel;

namespace DD2TextMod
{
    /// <summary>
    /// 进程内虚拟鼠标 + 虚拟键盘。
    /// 注意：这是 Input System 层的"假设备"，不会移动 Windows 光标，也不会抢你的键鼠。
    /// </summary>
    internal static class VirtualInput
    {
        private static Mouse _mouse;
        private static Keyboard _keyboard;

        public static Mouse MouseDev
        {
            get
            {
                if (_mouse == null || !_mouse.added)
                {
                    try { _mouse = InputSystem.AddDevice<Mouse>("DD2VirtualMouse"); }
                    catch (Exception e) { Plugin.Log?.LogError("add mouse failed: " + e.Message); return null; }
                }
                return _mouse;
            }
        }

        public static Keyboard KeyboardDev
        {
            get
            {
                if (_keyboard == null || !_keyboard.added)
                {
                    try { _keyboard = InputSystem.AddDevice<Keyboard>("DD2VirtualKeyboard"); }
                    catch (Exception e) { Plugin.Log?.LogError("add keyboard failed: " + e.Message); return null; }
                }
                return _keyboard;
            }
        }

        public static object MouseMove(float x, float y, bool left, bool right)
        {
            var m = MouseDev;
            if (m == null) return new { ok = false, error = "virtual mouse not available" };
            ushort buttons = 0;
            if (left) buttons |= 1;
            if (right) buttons |= 2;
            InputSystem.QueueStateEvent(m, new MouseState { position = new Vector2(x, y), buttons = buttons });
            return new { ok = true, x, y, left, right, device = m.deviceId };
        }

        /// <summary>按键：逗号分隔的 keysym，例如 "w" / "space" / "leftShift"。</summary>
        public static object Keys(string keys, bool up)
        {
            var kb = KeyboardDev;
            if (kb == null) return new { ok = false, error = "virtual keyboard not available" };
            var state = new KeyboardState();
            if (!up && !string.IsNullOrEmpty(keys))
            {
                foreach (var k in keys.Split(','))
                {
                    var key = ParseKey(k.Trim());
                    if (key != UnityEngine.InputSystem.Key.None) state.Press(key);
                }
            }
            InputSystem.QueueStateEvent(kb, state);
            return new { ok = true, keys, up, device = kb.deviceId };
        }

        private static UnityEngine.InputSystem.Key ParseKey(string s)
        {
            try
            {
                return (UnityEngine.InputSystem.Key)Enum.Parse(typeof(Key), s, true);
            }
            catch
            {
                switch (s.ToLower())
                {
                    case "space": return UnityEngine.InputSystem.Key.Space;
                    case "enter": return UnityEngine.InputSystem.Key.Enter;
                    case "esc": case "escape": return UnityEngine.InputSystem.Key.Escape;
                    case "w": return UnityEngine.InputSystem.Key.W;
                    case "a": return UnityEngine.InputSystem.Key.A;
                    case "s": return UnityEngine.InputSystem.Key.S;
                    case "d": return UnityEngine.InputSystem.Key.D;
                    case "left": return UnityEngine.InputSystem.Key.LeftArrow;
                    case "right": return UnityEngine.InputSystem.Key.RightArrow;
                    case "up": return UnityEngine.InputSystem.Key.UpArrow;
                    case "down": return UnityEngine.InputSystem.Key.DownArrow;
                    case "shift": return UnityEngine.InputSystem.Key.LeftShift;
                    default: return UnityEngine.InputSystem.Key.None;
                }
            }
        }

        public static object Info()
        {
            var m = MouseDev;
            var kb = KeyboardDev;
            return new
            {
                ok = m != null && kb != null,
                mouse = m == null ? null : new { id = m.deviceId, added = m.added, enabled = m.enabled, pos = m.position.ReadValue().ToString() },
                keyboard = kb == null ? null : new { id = kb.deviceId, added = kb.added, enabled = kb.enabled },
            };
        }
    }
}

