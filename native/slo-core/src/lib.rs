//! SLO processing core. The wire carries Python scalar representations and
//! references; algorithms and decisions live here. Ordered pairs, rather than
//! JSON object order, preserve Python/YAML insertion order at every output level.

use serde_json::{json, Value};
use std::cmp::Ordering;
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::slice;
use std::sync::Arc;

const ABI: u32 = 1;
type Result<T> = std::result::Result<T, Error>;

#[derive(Debug, Clone)]
struct Error {
    kind: String,
    message: String,
}

fn error(kind: &str, message: impl Into<String>) -> Error {
    Error {
        kind: kind.into(),
        message: message.into(),
    }
}
fn invalid(message: impl Into<String>) -> Error {
    error("ValueError", message)
}
fn protocol(message: impl Into<String>) -> Error {
    error("RuntimeError", message)
}

/// An input value. Original Python objects have ids; synthetic merge maps and
/// defaults have no id and are recursively materialized by output().
#[derive(Debug, Clone)]
struct Py(Value, Arc<Vec<Value>>);

impl Py {
    fn literal(value: Value) -> Self {
        Self(value, Arc::default())
    }
    fn resolve(value: Value, nodes: Arc<Vec<Value>>) -> Self {
        let value = value["node"]
            .as_u64()
            .and_then(|id| nodes.get(id as usize).cloned())
            .unwrap_or(value);
        Self(value, nodes)
    }
    fn child(&self, value: &Value) -> Self {
        Self::resolve(value.clone(), Arc::clone(&self.1))
    }
    fn kind(&self) -> &str {
        self.0["type"].as_str().unwrap_or("unknown")
    }
    fn string(&self) -> &str {
        self.0["str"].as_str().unwrap_or("")
    }
    fn repr(&self) -> &str {
        self.0["repr"].as_str().unwrap_or("")
    }
    fn representation(&self, name: &str) -> Result<&str> {
        if let Some(value) = self.0[name].as_str() {
            return Ok(value);
        }
        let failure = &self.0[format!("{name}_error")];
        Err(error(
            failure["type"].as_str().unwrap_or("RuntimeError"),
            failure["message"]
                .as_str()
                .unwrap_or("missing scalar representation"),
        ))
    }
    fn display(&self) -> Result<&str> {
        self.representation("str")
    }
    fn items(&self) -> Vec<Py> {
        self.0["items"]
            .as_array()
            .map(|a| a.iter().map(|v| self.child(v)).collect())
            .unwrap_or_default()
    }
    fn pairs(&self) -> Vec<(Py, Py)> {
        self.items()
            .into_iter()
            .map(|v| (self.child(&v.0[0]), self.child(&v.0[1])))
            .collect()
    }
    fn attribute_error(&self, name: &str) -> Error {
        error(
            "AttributeError",
            format!("'{}' object has no attribute '{name}'", self.kind()),
        )
    }
    fn get(&self, key: &str, default: Py) -> Result<Py> {
        if self.kind() != "dict" {
            return Err(self.attribute_error("get"));
        }
        Ok(self
            .pairs()
            .into_iter()
            .find(|(k, _)| k.kind() == "str" && k.string() == key)
            .map(|(_, v)| v)
            .unwrap_or(default))
    }
    fn at(&self, key: &str) -> Result<Py> {
        if self.kind() != "dict" {
            return Err(error(
                "TypeError",
                match self.kind() {
                    "str" => "string indices must be integers, not 'str'".into(),
                    "list" | "tuple" => format!(
                        "{} indices must be integers or slices, not str",
                        self.kind()
                    ),
                    _ => format!("'{}' object is not subscriptable", self.kind()),
                },
            ));
        }
        self.pairs()
            .into_iter()
            .find(|(k, _)| k.kind() == "str" && k.string() == key)
            .map(|(_, v)| v)
            .ok_or_else(|| error("KeyError", key))
    }
    fn contains(&self, key: &str) -> Result<bool> {
        match self.kind() {
            "dict" => Ok(self
                .pairs()
                .iter()
                .any(|(k, _)| k.kind() == "str" && k.string() == key)),
            "str" => Ok(self.string().contains(key)),
            "list" | "tuple" => Ok(self
                .items()
                .iter()
                .any(|v| v.kind() == "str" && v.string() == key)),
            "bytes" => Err(error(
                "TypeError",
                "a bytes-like object is required, not 'str'",
            )),
            _ => Err(error(
                "TypeError",
                format!("argument of type '{}' is not iterable", self.kind()),
            )),
        }
    }
    fn iter(&self) -> Result<Vec<Py>> {
        match self.kind() {
            "dict" => Ok(self.pairs().into_iter().map(|(k, _)| k).collect()),
            "list" | "tuple" => Ok(self.items()),
            "str" => Ok(self
                .string()
                .chars()
                .map(|c| pystr(&c.to_string()))
                .collect()),
            _ => Err(error(
                "TypeError",
                format!("'{}' object is not iterable", self.kind()),
            )),
        }
    }
    fn number(&self) -> Result<f64> {
        let value = &self.0["number"];
        if let Some(bits) = value["bits"].as_str() {
            return u64::from_str_radix(bits, 16)
                .map(f64::from_bits)
                .map_err(|_| protocol("invalid float bits"));
        }
        let e = &value["error"];
        Err(error(
            e["type"].as_str().unwrap_or("RuntimeError"),
            e["message"]
                .as_str()
                .unwrap_or("invalid numeric wire value"),
        ))
    }
    fn output(&self) -> Value {
        if let Some(id) = self.0["id"].as_u64() {
            return json!({"ref": id});
        }
        match self.kind() {
            "dict" => object(
                self.pairs()
                    .into_iter()
                    .map(|(k, v)| (k.output(), v.output()))
                    .collect(),
            ),
            "list" | "tuple" => array(self.items().iter().map(Py::output).collect()),
            "NoneType" => json!({"none": true}),
            _ => string(self.string()),
        }
    }
}

fn pystr(s: &str) -> Py {
    Py::literal(json!({"type":"str", "str":s, "repr":format!("{s:?}")}))
}
fn map(pairs: Vec<(Py, Py)>) -> Py {
    let nodes = pairs
        .iter()
        .flat_map(|(k, v)| [k, v])
        .find(|p| !p.1.is_empty())
        .map(|p| Arc::clone(&p.1))
        .unwrap_or_default();
    Py(
        json!({"type":"dict", "str":"{}", "repr":"{}", "items":
        pairs.into_iter().map(|(k,v)| vec![k.0,v.0]).collect::<Vec<_>>()}),
        nodes,
    )
}
fn empty() -> Py {
    map(vec![])
}
fn list() -> Py {
    Py::literal(json!({"type":"list", "str":"[]", "repr":"[]", "items":[]}))
}
fn none() -> Py {
    Py::literal(json!({"type":"NoneType", "str":"None", "repr":"None"}))
}
fn string(s: &str) -> Value {
    json!({"str": s})
}
fn array(values: Vec<Value>) -> Value {
    json!({"list":values})
}
fn object(pairs: Vec<(Value, Value)>) -> Value {
    json!({"dict":pairs.into_iter().map(|(k,v)| vec![k,v]).collect::<Vec<_>>()})
}
fn dict(pairs: Vec<(&str, Value)>) -> Value {
    object(pairs.into_iter().map(|(k, v)| (string(k), v)).collect())
}
fn formatted(value: f64, precision: u8) -> Value {
    json!({"format":format!("{:016x}", value.to_bits()), "precision":precision})
}
fn text(parts: Vec<Value>) -> Value {
    json!({"text":parts})
}

fn equal(a: &Py, b: &Py) -> bool {
    if a.kind() == "str" && b.kind() == "str" {
        return a.string() == b.string();
    }
    if a.0["id"].is_number() && a.0["id"] == b.0["id"] {
        return true;
    }
    if ["int", "float", "bool"].contains(&a.kind()) && ["int", "float", "bool"].contains(&b.kind())
    {
        if a.kind() == "float" && b.kind() == "float" {
            return matches!((a.number(), b.number()), (Ok(x), Ok(y)) if x == y);
        }
        return matches!((a.0["integer"].as_str(), b.0["integer"].as_str()),
            (Some(x), Some(y)) if x == y);
    }
    if a.kind() == "tuple" && b.kind() == "tuple" {
        let (a, b) = (a.items(), b.items());
        return a.len() == b.len() && a.iter().zip(&b).all(|(a, b)| equal(a, b));
    }
    a.kind() == b.kind() && a.0["repr"].is_string() && a.repr() == b.repr()
}

fn as_dict(value: &Py) -> Result<Vec<(Py, Py)>> {
    if value.kind() == "dict" {
        return Ok(value.pairs());
    }
    let mut pairs = vec![];
    for (index, entry) in value.iter()?.iter().enumerate() {
        let items = entry.iter().map_err(|_| {
            error(
                "TypeError",
                format!("cannot convert dictionary update sequence element #{index} to a sequence"),
            )
        })?;
        if items.len() != 2 {
            return Err(invalid(format!(
                "dictionary update sequence element #{index} has length {}; 2 is required",
                items.len()
            )));
        }
        update(&mut pairs, vec![(items[0].clone(), items[1].clone())])?;
    }
    Ok(pairs)
}

fn update(pairs: &mut Vec<(Py, Py)>, additions: Vec<(Py, Py)>) -> Result<()> {
    fn check_key(key: &Py) -> Result<()> {
        if ["list", "dict", "set"].contains(&key.kind()) {
            return Err(error(
                "TypeError",
                format!("unhashable type: '{}'", key.kind()),
            ));
        }
        if key.kind() == "tuple" {
            for child in key.items() {
                check_key(&child)?;
            }
        }
        Ok(())
    }
    for (key, value) in additions {
        check_key(&key)?;
        if let Some((_, v)) = pairs.iter_mut().find(|(k, _)| equal(k, &key)) {
            *v = value;
        } else {
            pairs.push((key, value));
        }
    }
    Ok(())
}

fn merged_alerting(spec: &Py, slo: &Py) -> Result<Py> {
    let base = spec.get("defaults", empty())?.get("alerting", empty())?;
    let overrides = slo.get("alerting", empty())?;
    let mut output = vec![];
    for severity in ["page", "ticket"] {
        let mut merged = vec![];
        update(&mut merged, as_dict(&base.get(severity, empty())?)?)?;
        update(&mut merged, as_dict(&overrides.get(severity, empty())?)?)?;
        let cfg = map(merged);
        if !cfg.contains("windows")? {
            return Err(invalid(format!("no {severity} windows defined")));
        }
        output.push((pystr(severity), cfg));
    }
    Ok(map(output))
}

/// Decimal strings keep duration parsing and ordering unbounded, as Python int
/// is. Unicode Nd normalization is a character property supplied by the wire.
fn duration(value: &Py) -> Result<String> {
    let source = value.0["decimal"].as_str().unwrap_or(value.display()?);
    let source = source.strip_suffix('\n').unwrap_or(source);
    let bad = || -> Result<Error> {
        Ok(invalid(format!(
            "bad duration {}; expected e.g. 5m, 1h, 30d",
            value.representation("repr")?
        )))
    };
    if source.len() < 2 || !source.as_bytes().last().is_some_and(u8::is_ascii) {
        return Err(bad()?);
    }
    let (digits, unit) = source.split_at(source.len() - 1);
    let multiplier = match unit {
        "s" => 1,
        "m" => 60,
        "h" => 3600,
        "d" => 86400,
        "w" => 604800,
        _ => return Err(bad()?),
    };
    if digits.is_empty() || !digits.bytes().all(|b| b.is_ascii_digit()) {
        return Err(bad()?);
    }
    // Python 3.13's int decimal conversion limit is carried as wire metadata.
    if let Some(message) = value.0["int_error"].as_str() {
        return Err(invalid(message));
    }
    let mut carry = 0_u32;
    let mut result = vec![];
    for digit in digits.bytes().rev() {
        carry += u32::from(digit - b'0') * multiplier;
        result.push((carry % 10) as u8 + b'0');
        carry /= 10;
    }
    while carry != 0 {
        result.push((carry % 10) as u8 + b'0');
        carry /= 10;
    }
    while result.len() > 1 && result.last() == Some(&b'0') {
        result.pop();
    }
    result.reverse();
    Ok(String::from_utf8(result).expect("ASCII duration"))
}
fn decimal_cmp(a: &str, b: &str) -> Ordering {
    a.len().cmp(&b.len()).then_with(|| a.cmp(b))
}

fn validate(spec: &Py) -> Result<()> {
    if spec.get("apiVersion", none())?.string() != "lab/v1" {
        return Err(invalid("apiVersion must be lab/v1"));
    }
    let mut names = vec![];
    for slo in spec.get("slos", list())?.iter()? {
        for key in ["name", "service", "objective", "sli"] {
            if !slo.contains(key)? {
                return Err(invalid(format!("slo missing '{key}': {}", slo.display()?)));
            }
        }
        let name = slo.at("name")?;
        if name.kind() != "str" {
            return Err(error(
                "TypeError",
                if name.kind() == "bytes" {
                    "cannot use a string pattern on a bytes-like object".into()
                } else {
                    format!(
                        "expected string or bytes-like object, got '{}'",
                        name.kind()
                    )
                },
            ));
        }
        let bytes = name.string().as_bytes();
        if bytes.is_empty()
            || !(bytes[0].is_ascii_lowercase() || bytes[0].is_ascii_digit())
            || !bytes
                .iter()
                .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || *b == b'-')
        {
            return Err(invalid(format!(
                "slo name must be lowercase kebab-case: {}",
                name.representation("repr")?
            )));
        }
        if names.contains(&name.string().to_owned()) {
            return Err(invalid(format!(
                "duplicate slo name {}",
                name.representation("repr")?
            )));
        }
        names.push(name.string().to_owned());
        let objective = slo.at("objective")?;
        let ratio = objective.number()?;
        if !(0.0 < ratio && ratio < 100.0) {
            return Err(invalid(format!(
                "objective must be a percentage in (0, 100): {}",
                objective.display()?
            )));
        }
        let sli = slo.at("sli")?;
        if sli.get("type", none())?.string() != "ratio" {
            return Err(invalid("only sli.type=ratio is supported"));
        }
        for key in ["errors", "total"] {
            if !sli.get(key, pystr(""))?.contains("{{window}}")? {
                return Err(invalid(format!(
                    "sli.{key} must contain the [{{{{window}}}}] placeholder"
                )));
            }
        }
    }
    let alerting = merged_alerting(spec, &empty())?;
    for severity in ["page", "ticket"] {
        for window in alerting.at(severity)?.at("windows")?.iter()? {
            let long = duration(&window.at("long")?)?;
            let short = duration(&window.at("short")?)?;
            if decimal_cmp(&long, &short) != Ordering::Greater {
                return Err(invalid(format!(
                    "long window must exceed short window: {}",
                    window.display()?
                )));
            }
            if window.at("burn_rate")?.number()? <= 0.0 {
                return Err(invalid(format!(
                    "burn_rate must be positive: {}",
                    window.display()?
                )));
            }
        }
    }
    Ok(())
}

fn render(query: &Py, window: &Py) -> Result<String> {
    if query.kind() == "bytes" {
        return Err(error(
            "TypeError",
            "a bytes-like object is required, not 'str'",
        ));
    }
    if query.kind() != "str" {
        return Err(query.attribute_error("replace"));
    }
    if window.kind() != "str" {
        return Err(error(
            "TypeError",
            format!(
                "replace() argument 2 must be str, not {}",
                if window.kind() == "NoneType" {
                    "None"
                } else {
                    window.kind()
                }
            ),
        ));
    }
    Ok(query
        .string()
        .replace("{{window}}", window.string())
        .split(|c: char| c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c))
        .filter(|s| !s.is_empty())
        .collect::<Vec<_>>()
        .join(" "))
}

fn slo_window(spec: &Py, slo: &Py) -> Result<Py> {
    // Evaluate the default eagerly, just like dict.get(key, expression).
    slo.get(
        "window",
        spec.get("defaults", empty())?.get("window", pystr("30d"))?,
    )
}

fn recording_rules(spec: &Py, slo: &Py) -> Result<Value> {
    let objective = slo.at("objective")?.number()? / 100.0;
    let budget = 1.0 - objective;
    let period = slo_window(spec, slo)?;
    let alerting = merged_alerting(spec, slo)?;
    let mut windows: Vec<Py> = vec![];
    for severity in ["page", "ticket"] {
        for window in alerting.at(severity)?.at("windows")?.iter()? {
            for key in ["long", "short"] {
                let value = window.at(key)?;
                if !windows.iter().any(|v| equal(v, &value)) {
                    windows.push(value);
                }
            }
        }
    }
    let mut keyed = vec![];
    for w in windows {
        keyed.push((duration(&w)?, w));
    }
    keyed.sort_by(|(a, _), (b, _)| decimal_cmp(a, b)); // stable equal-duration order
    let mut windows: Vec<Py> = keyed.into_iter().map(|(_, w)| w).collect();
    if !windows.iter().any(|v| equal(v, &period)) {
        windows.push(period.clone());
    }
    let labels = || -> Result<Value> {
        Ok(dict(vec![
            ("slo", slo.at("name")?.output()),
            ("service", slo.at("service")?.output()),
        ]))
    };
    let rule = |name: String, expr: Value| -> Result<Value> {
        Ok(dict(vec![
            ("record", string(&name)),
            ("expr", expr),
            ("labels", labels()?),
        ]))
    };
    let mut rules = vec![
        rule(
            "slo:objective:ratio".into(),
            text(vec![
                string("vector("),
                formatted(objective, 6),
                string(")"),
            ]),
        )?,
        rule(
            "slo:error_budget:ratio".into(),
            text(vec![string("vector("), formatted(budget, 6), string(")")]),
        )?,
    ];
    for window in windows {
        let errors = render(&slo.at("sli")?.at("errors")?, &window)?;
        let total = render(&slo.at("sli")?.at("total")?, &window)?;
        rules.push(rule(
            format!("slo:sli_error:ratio_rate{}", window.display()?),
            string(&format!("(({errors}) or vector(0)) / ({total})")),
        )?);
        rules.push(rule(
            format!("slo:burn_rate:ratio_rate{}", window.display()?),
            text(vec![
                string(&format!(
                    "slo:sli_error:ratio_rate{}{{slo=\"{}\"}} / ",
                    window.display()?,
                    slo.at("name")?.display()?
                )),
                formatted(budget, 6),
            ]),
        )?);
    }
    rules.push(rule(
        "slo:error_budget_remaining:ratio".into(),
        text(vec![
            string(&format!(
                "1 - (slo:sli_error:ratio_rate{}{{slo=\"{}\"}} / ",
                period.display()?,
                slo.at("name")?.display()?
            )),
            formatted(budget, 6),
            string(")"),
        ]),
    )?);
    Ok(array(rules))
}

fn alert_rules(spec: &Py, slo: &Py) -> Result<Value> {
    let objective = slo.at("objective")?.number()? / 100.0;
    let budget = 1.0 - objective;
    let name = slo.at("name")?;
    let alerting = merged_alerting(spec, slo)?;
    let mut rules = vec![];
    for severity in ["page", "ticket"] {
        let cfg = alerting.at(severity)?;
        let mut clauses = vec![];
        for w in cfg.at("windows")?.iter()? {
            let br = w.at("burn_rate")?.number()?;
            if !clauses.is_empty() {
                clauses.push(string("\n  or\n  "));
            }
            clauses.extend([
                string(&format!(
                    "(\n    slo:burn_rate:ratio_rate{}{{slo=\"{}\"}} > ",
                    w.at("long")?.display()?,
                    name.display()?
                )),
                formatted(br, 6),
                string("\n    and\n"),
                string(&format!(
                    "    slo:burn_rate:ratio_rate{}{{slo=\"{}\"}} > ",
                    w.at("short")?.display()?,
                    name.display()?
                )),
                formatted(br, 6),
                string("\n  )"),
            ]);
        }
        let windows_text = cfg
            .at("windows")?
            .iter()?
            .iter()
            .map(|w| {
                Ok(format!(
                    "{}x over {}/{}",
                    w.at("burn_rate")?.display()?,
                    w.at("long")?.display()?,
                    w.at("short")?.display()?
                ))
            })
            .collect::<Result<Vec<_>>>()?
            .join(", ");
        if name.kind() == "bytes" {
            return Err(error(
                "TypeError",
                "a bytes-like object is required, not 'str'",
            ));
        }
        if name.kind() != "str" {
            return Err(name.attribute_error("split"));
        }
        let title = name.0["capitalize_parts"]
            .as_str()
            .map(str::to_owned)
            .unwrap_or_else(|| {
                name.string()
                    .split('-')
                    .map(|p| {
                        let mut c = p.chars();
                        c.next()
                            .map(|first| {
                                first.to_uppercase().collect::<String>()
                                    + &c.as_str().to_lowercase()
                            })
                            .unwrap_or_default()
                    })
                    .collect::<String>()
            });
        let alert_name = title + "BurnRate" + if severity == "page" { "Page" } else { "Ticket" };
        let label_values = dict(vec![
            ("severity", string(severity)),
            ("service", slo.at("service")?.output()),
            ("slo", name.output()),
            ("team", slo.get("team", pystr("platform-sre"))?.output()),
        ]);
        let description = text(vec![
            string(&format!("{}. Objective {}% (error budget ", slo.get("description", name.clone())?.display()?, slo.at("objective")?.display()?)),
            formatted(budget * 100.0, 3),
            string(&format!("% of requests over {}). Both windows of at least one pair ({windows_text}) exceed their burn-rate threshold. Check per-window burn and recent errors on the dashboard; the runbook lists the first diagnostic queries.", slo_window(spec, slo)?.display()?)),
        ]);
        let annotations = dict(vec![
            (
                "summary",
                string(&format!(
                    "{}: {} is burning error budget ({windows_text})",
                    if severity == "page" { "Page" } else { "Ticket" },
                    name.string()
                )),
            ),
            ("description", description),
            ("runbook_url", slo.at("runbook_url")?.output()),
            ("dashboard_url", slo.at("dashboard_url")?.output()),
        ]);
        rules.push(dict(vec![
            ("alert", string(&alert_name)),
            ("expr", text(clauses)),
            ("for", cfg.get("for", pystr("0m"))?.output()),
            ("labels", label_values),
            ("annotations", annotations),
        ]));
    }
    Ok(array(rules))
}

fn generate(spec: &Py) -> Result<Value> {
    validate(spec)?;
    let mut groups = vec![];
    for slo in spec.at("slos")?.iter()? {
        let recording_name = format!("slo-{}-recording", slo.at("name")?.display()?);
        groups.push(dict(vec![
            ("name", string(&recording_name)),
            ("rules", recording_rules(spec, &slo)?),
        ]));
        let alerts_name = format!("slo-{}-alerts", slo.at("name")?.display()?);
        groups.push(dict(vec![
            ("name", string(&alerts_name)),
            ("rules", alert_rules(spec, &slo)?),
        ]));
    }
    Ok(dict(vec![("groups", array(groups))]))
}

fn process(request: Value) -> Result<Value> {
    if request["abi"].as_u64() != Some(u64::from(ABI)) {
        return Err(protocol("SLO request ABI mismatch"));
    }
    let op = request["op"]
        .as_str()
        .ok_or_else(|| protocol("missing SLO operation"))?;
    let args = request["args"]
        .as_array()
        .ok_or_else(|| protocol("missing SLO arguments"))?;
    let arity = match op {
        "generate" | "validate" | "duration" | "metric_name" => 1,
        "merged" | "recording" | "alerts" | "render" => 2,
        _ => return Err(protocol("unknown SLO operation")),
    };
    if args.len() != arity {
        return Err(protocol("wrong SLO argument count"));
    }
    let nodes = Arc::new(request["nodes"].as_array().cloned().unwrap_or_default());
    let a = Py::resolve(args[0].clone(), Arc::clone(&nodes));
    let b = args
        .get(1)
        .map(|v| Py::resolve(v.clone(), nodes))
        .unwrap_or_else(none);
    match op {
        "generate" => generate(&a),
        "validate" => {
            validate(&a)?;
            Ok(json!({"none":true}))
        }
        "duration" => Ok(json!({"integer":duration(&a)?})),
        "merged" => Ok(merged_alerting(&a, &b)?.output()),
        "recording" => recording_rules(&a, &b),
        "alerts" => alert_rules(&a, &b),
        "render" => Ok(string(&render(&a, &b)?)),
        "metric_name" => {
            if a.kind() == "bytes" {
                return Err(error(
                    "TypeError",
                    "a bytes-like object is required, not 'str'",
                ));
            }
            if a.kind() != "str" {
                return Err(a.attribute_error("replace"));
            }
            Ok(string(&a.string().replace('-', "_")))
        }
        _ => unreachable!(),
    }
}

fn response(bytes: &[u8]) -> Vec<u8> {
    let result = serde_json::from_slice(bytes)
        .map_err(|e| protocol(format!("invalid SLO wire JSON: {e}")))
        .and_then(process);
    let envelope = match result {
        Ok(value) => json!({"abi":ABI,"value":value}),
        Err(e) => json!({"abi":ABI,"error":{"type":e.kind,"message":e.message}}),
    };
    serde_json::to_vec(&envelope).expect("JSON response is serializable")
}

#[no_mangle]
pub extern "C" fn slo_core_abi_version() -> u32 {
    ABI
}

/// One call per complete operation/spec. Input is borrowed only during this
/// call. The returned allocation belongs to Rust until slo_core_free(ptr,len).
///
/// # Safety
/// input must address input_len readable bytes; output_len must be writable.
/// Null input is accepted only for length zero. No Rust panic crosses the ABI.
#[no_mangle]
pub unsafe extern "C" fn slo_core_call(
    input: *const u8,
    input_len: usize,
    output_len: *mut usize,
) -> *mut u8 {
    if output_len.is_null() {
        return std::ptr::null_mut();
    }
    *output_len = 0;
    if input.is_null() && input_len != 0 {
        return std::ptr::null_mut();
    }
    let result = catch_unwind(AssertUnwindSafe(|| {
        let bytes = if input_len == 0 {
            &[]
        } else {
            slice::from_raw_parts(input, input_len)
        };
        response(bytes)
    }))
    .unwrap_or_else(|_| {
        br#"{"abi":1,"error":{"type":"RuntimeError","message":"SLO core panic"}}"#.to_vec()
    });
    let mut buffer = result.into_boxed_slice();
    *output_len = buffer.len();
    let pointer = buffer.as_mut_ptr();
    std::mem::forget(buffer);
    pointer
}

/// # Safety
/// ptr/len must be an outstanding pair returned by slo_core_call, freed once.
/// A null pointer is a no-op. Python releases the buffer in a finally block.
#[no_mangle]
pub unsafe extern "C" fn slo_core_free(ptr: *mut u8, len: usize) {
    if !ptr.is_null() {
        drop(Box::from_raw(std::ptr::slice_from_raw_parts_mut(ptr, len)));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn numeric_key_comparisons_do_not_round_integers() {
        fn integer(value: &str) -> Py {
            Py::literal(json!({"type":"int", "integer":value}))
        }
        let a = integer("0x20000000000000");
        let b = integer("0x20000000000001");
        let float = Py::literal(json!({"type":"float", "integer":"0x20000000000000",
            "number":{"bits":"4340000000000000"}}));
        assert!(!equal(&a, &b));
        assert!(equal(&a, &float));
        assert!(!equal(&b, &float));
        let mut pairs = vec![(a, pystr("old"))];
        update(&mut pairs, vec![(b, pystr("new"))]).unwrap();
        assert_eq!(pairs.len(), 2);
        assert_eq!(pairs[0].1.string(), "old");
        assert_eq!(pairs[1].1.string(), "new");
    }

    #[test]
    fn graph_cycles_and_unread_representation_errors_preserve_validation_order() {
        let nodes = Arc::new(vec![
            json!({"id":0,"type":"dict", "items":[
                [{"node":1},{"node":2}], [{"node":3},{"node":4}]],
                "str_error":{"type":"ValueError","message":"unread metadata"}}),
            json!({"id":1,"type":"str","str":"apiVersion"}),
            json!({"id":2,"type":"str","str":"wrong"}),
            json!({"id":3,"type":"str","str":"metadata"}),
            json!({"id":4,"type":"list","items":[{"node":4}]}),
        ]);
        let source = Py::resolve(json!({"node":0}), nodes);
        let failure = validate(&source).unwrap_err();
        assert_eq!(failure.kind, "ValueError");
        assert_eq!(failure.message, "apiVersion must be lab/v1");
        let metadata = source.at("metadata").unwrap();
        assert_eq!(metadata.items()[0].output(), json!({"ref":4}));
        assert_eq!(metadata.output(), json!({"ref":4}));
        assert_eq!(source.display().unwrap_err().message, "unread metadata");
    }

    #[test]
    fn complete_spec_constructs_recording_then_alert_groups() {
        fn number(n: f64) -> Py {
            Py::literal(
                json!({"type":"float","str":n.to_string(),"repr":n.to_string(),
                "number":{"bits":format!("{:016x}",n.to_bits())}}),
            )
        }
        fn input_list(items: Vec<Py>) -> Py {
            Py::literal(
                json!({"type":"list","items":items.into_iter().map(|p| p.0).collect::<Vec<_>>()}),
            )
        }
        fn input_map(items: Vec<(&str, Py)>) -> Py {
            map(items.into_iter().map(|(k, v)| (pystr(k), v)).collect())
        }
        fn member<'a>(value: &'a Value, key: &str) -> &'a Value {
            &value["dict"]
                .as_array()
                .unwrap()
                .iter()
                .find(|p| p[0]["str"] == key)
                .unwrap()[1]
        }
        let window = input_map(vec![
            ("long", pystr("1h")),
            ("short", pystr("5m")),
            ("burn_rate", number(14.4)),
        ]);
        let ladder = input_map(vec![("windows", input_list(vec![window]))]);
        let slo = input_map(vec![
            ("name", pystr("synthetic-api")),
            ("service", pystr("synthetic")),
            ("objective", number(99.9)),
            (
                "sli",
                input_map(vec![
                    ("type", pystr("ratio")),
                    ("errors", pystr("errors[{{window}}]")),
                    ("total", pystr("total[{{window}}]")),
                ]),
            ),
            ("runbook_url", pystr("https://example.invalid/runbook")),
            ("dashboard_url", pystr("https://example.invalid/dashboard")),
        ]);
        let spec = input_map(vec![
            ("apiVersion", pystr("lab/v1")),
            (
                "defaults",
                input_map(vec![(
                    "alerting",
                    input_map(vec![("page", ladder.clone()), ("ticket", ladder)]),
                )]),
            ),
            ("slos", input_list(vec![slo])),
        ]);
        let rules = generate(&spec).unwrap();
        let groups = member(&rules, "groups")["list"].as_array().unwrap();
        assert_eq!(groups.len(), 2);
        assert_eq!(
            member(&groups[0], "name")["str"],
            "slo-synthetic-api-recording"
        );
        assert_eq!(
            member(&groups[1], "name")["str"],
            "slo-synthetic-api-alerts"
        );
        let records = member(&groups[0], "rules")["list"].as_array().unwrap();
        assert_eq!(records.len(), 9); // objective, budget, 3 windows * 2, remaining
        assert_eq!(
            member(&records[2], "record")["str"],
            "slo:sli_error:ratio_rate5m"
        );
        assert_eq!(
            member(&records[2], "expr")["str"],
            "((errors[5m]) or vector(0)) / (total[5m])"
        );
        let alerts = member(&groups[1], "rules")["list"].as_array().unwrap();
        assert_eq!(
            member(&alerts[0], "alert")["str"],
            "SyntheticApiBurnRatePage"
        );
        assert_eq!(
            member(&alerts[1], "alert")["str"],
            "SyntheticApiBurnRateTicket"
        );
    }

    #[test]
    fn unbounded_duration_and_python_terminal_newline() {
        let n = "9".repeat(100);
        assert_eq!(
            duration(&pystr(&(n + "m"))).unwrap(),
            "5".to_owned() + &"9".repeat(99) + "40"
        );
        assert_eq!(duration(&pystr("0001h\n")).unwrap(), "3600");
        assert!(duration(&pystr("1h\n\n")).is_err());
        assert!(duration(&pystr("²h")).is_err());
    }

    #[test]
    fn wire_errors_are_contained() {
        for request in [
            b"\xff".as_slice(),
            b"{",
            b"null",
            b"{}",
            br#"{"abi":1,"op":"oops","args":[]}"#,
        ] {
            let result: Value = serde_json::from_slice(&response(request)).unwrap();
            assert_eq!(result["error"]["type"], "RuntimeError");
        }
    }

    #[test]
    fn native_allocation_and_null_contract() {
        unsafe {
            assert!(slo_core_call(std::ptr::null(), 0, std::ptr::null_mut()).is_null());
            let mut len = 99;
            assert!(slo_core_call(std::ptr::null(), 1, &mut len).is_null());
            assert_eq!(len, 0);
            for _ in 0..64 {
                let ptr = slo_core_call(std::ptr::null(), 0, &mut len);
                assert!(!ptr.is_null());
                assert!(serde_json::from_slice::<Value>(slice::from_raw_parts(ptr, len)).is_ok());
                slo_core_free(ptr, len);
            }
            slo_core_free(std::ptr::null_mut(), 0);
        }
    }

    #[test]
    fn python_query_whitespace_and_stable_duration_ties() {
        assert_eq!(
            render(&pystr(" a\u{1c}\u{2003}{{window}}\n b "), &pystr("1h")).unwrap(),
            "a 1h b"
        );
        let mut windows = ["1h", "60m", "5m"].map(|w| (duration(&pystr(w)).unwrap(), w));
        windows.sort_by(|(a, _), (b, _)| decimal_cmp(a, b));
        assert_eq!(windows.map(|(_, w)| w), ["5m", "1h", "60m"]);
    }
}
