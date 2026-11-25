using System;
using System.Collections.Generic;
using System.Linq;
using System.Reflection;
using System.Text;

namespace DD2TextMod
{
    /// <summary>反射检查工具：把某个类型的字段/属性/方法、或某个活对象的字段值 dump 成文字。</summary>
    internal static class Inspect
    {
        private const BindingFlags All = BindingFlags.Instance | BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic;

        public static object DumpType(string name)
        {
            var t = FindType(name);
            if (t == null) return new { ok = false, error = "type not found: " + name };

            var fields = new List<object>();
            foreach (var f in t.GetFields(All))
            {
                object v = null;
                if (f.IsStatic)
                {
                    try { v = f.GetValue(null); } catch { }
                }
                fields.Add(new { n = f.Name, t = Short(f.FieldType), s = f.IsStatic, v = Val(v) });
            }
            var props = new List<object>();
            foreach (var p in t.GetProperties(All))
            {
                object v = null;
                if (p.GetGetMethod(true) != null && p.GetIndexParameters().Length == 0)
                {
                    try
                    {
                        if (p.GetGetMethod(true).IsStatic) v = p.GetValue(null);
                    }
                    catch { }
                }
                props.Add(new { n = p.Name, t = Short(p.PropertyType), v = Val(v) });
            }
            var methods = t.GetMethods(All).Where(m => !m.IsSpecialName)
                .Select(m => m.Name + "(" + string.Join(",", m.GetParameters().Select(p => Short(p.ParameterType))) + ")")
                .Distinct().ToList();

            return new { ok = true, type = t.FullName, baseType = Short(t.BaseType), fields, props, methods };
        }

        public static object DumpObject(string typeName, string fieldName)
        {
            var t = FindType(typeName);
            if (t == null) return new { ok = false, error = "type not found: " + typeName };
            var inst = t.GetProperty("Instance", All)?.GetValue(null);
            var owner = inst ?? t.GetField("s_instance", All)?.GetValue(null);
            if (owner == null) return new { ok = false, error = "instance not found for " + typeName };

            object target = owner;
            if (!string.IsNullOrEmpty(fieldName))
            {
                target = null;
                var f = owner.GetType().GetField(fieldName, All);
                if (f != null) target = f.GetValue(owner);
                if (target == null) return new { ok = false, error = "field not found or null: " + fieldName };
            }

            var vals = new List<object>();
            foreach (var f in target.GetType().GetFields(All))
            {
                object v = null;
                try { v = f.GetValue(target); } catch { }
                if (v is UnityEngine.Object uo && uo == null) v = "<destroyed>";
                vals.Add(new { n = f.Name, t = Short(f.FieldType), v = Val(v) });
            }
            return new { ok = true, type = target.GetType().FullName, fields = vals };
        }

        public static Type FindType(string name)
        {
            foreach (var asm in AppDomain.CurrentDomain.GetAssemblies())
            {
                Type[] types;
                try { types = asm.GetTypes(); } catch { continue; }
                foreach (var t in types)
                {
                    if (t.Name == name || t.FullName == name) return t;
                }
            }
            return null;
        }

        private static string Short(Type t)
        {
            if (t == null) return "null";
            if (!t.IsGenericType) return t.Name;
            return t.Name.Split('`')[0] + "<" + string.Join(",", t.GetGenericArguments().Select(Short)) + ">";
        }

        private static object Val(object v)
        {
            if (v == null) return null;
            var t = v.GetType();
            if (t.IsPrimitive || v is string) return v;
            if (v is UnityEngine.Object uo) return uo == null ? (object)"<destroyed>" : uo.name + "#" + uo.GetInstanceID();
            if (t.IsEnum) return v.ToString();
            if (v is System.Collections.ICollection c) return "count=" + c.Count;
            return v.ToString();
        }
    }
}
