/*
 * Login forms of the closed network (v1.0.0 U8, mockups.md M2 / M3).
 *
 * - [data-busy-form]: on submit the button is disabled and shows its
 *   data-busy-label, so the form is not sent twice.
 * - [data-password-rules]: while typing the new password, each rule of
 *   #password-rules shows met / not met with a symbol and text (not colour
 *   only). The list is not a live region; errors are announced only after a
 *   submission (role="alert" rendered by the server).
 *
 * The checks mirror src/models/password_policy.py; the server checks again
 * before calling Cognito, so this is only a hint.
 */
(function () {
    "use strict";

    var SYMBOLS = "^$*.[]{}()?-\"!@#%&/\\,><':;|_~`+=";

    function hasChar(value, test) {
        for (var i = 0; i < value.length; i += 1) {
            if (test(value.charAt(i))) {
                return true;
            }
        }
        return false;
    }

    function ruleMet(rule, value, form) {
        var min = parseInt(form.getAttribute("data-min-length"), 10) || 0;
        var max = parseInt(form.getAttribute("data-max-length"), 10) || 256;
        switch (rule) {
            case "min_length":
                return value.length >= min;
            case "max_length":
                return value.length <= max;
            case "uppercase":
                return hasChar(value, function (c) { return c >= "A" && c <= "Z"; });
            case "lowercase":
                return hasChar(value, function (c) { return c >= "a" && c <= "z"; });
            case "digits":
                return hasChar(value, function (c) { return c >= "0" && c <= "9"; });
            case "symbols":
                return hasChar(value, function (c) { return SYMBOLS.indexOf(c) >= 0; }) ||
                    value.slice(1, -1).indexOf(" ") >= 0;
            case "no_surrounding_space":
                return !/^\s/.test(value) && !/\s$/.test(value);
            default:
                return false;
        }
    }

    function showRule(item, met) {
        item.setAttribute("data-met", met ? "true" : "false");
        var mark = item.querySelector("[data-rule-mark]");
        var status = item.querySelector("[data-rule-status]");
        if (mark) {
            mark.textContent = met ? "✓" : "✗";
        }
        if (status) {
            status.textContent = met ? "（満たしている）" : "（満たしていない）";
        }
        item.classList.toggle("text-green-800", met);
        item.classList.toggle("text-gray-700", !met);
    }

    function wireRules(form) {
        var input = form.querySelector("[data-password-input]");
        if (!input) {
            return;
        }
        input.addEventListener("input", function () {
            var items = form.querySelectorAll("#password-rules [data-rule]");
            for (var i = 0; i < items.length; i += 1) {
                var rule = items[i].getAttribute("data-rule");
                showRule(items[i], ruleMet(rule, input.value, form));
            }
        });
    }

    function wireBusy(form) {
        form.addEventListener("submit", function (event) {
            var button = form.querySelector("button[type=submit]");
            if (!button) {
                return;
            }
            if (button.disabled) {
                event.preventDefault();
                return;
            }
            // Disable after the browser has captured the submission.
            window.setTimeout(function () {
                button.disabled = true;
                button.textContent = button.getAttribute("data-busy-label") || button.textContent;
            }, 0);
        });
    }

    function init() {
        var busy = document.querySelectorAll("form[data-busy-form]");
        for (var i = 0; i < busy.length; i += 1) {
            wireBusy(busy[i]);
        }
        var rules = document.querySelectorAll("form[data-password-rules]");
        for (var j = 0; j < rules.length; j += 1) {
            wireRules(rules[j]);
        }
    }

    if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", init);
    } else {
        init();
    }
})();
