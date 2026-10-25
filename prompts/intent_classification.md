# Deletion-intent classification rules

**No model receives this file.** `code/ghost_audit/intent.py` decides deletion intent with
deterministic, explainable rules, and the full-dataset run makes zero model calls. This document
is a specification reconstructed from `intent.py`. It is not a verbatim historical prompt: no prompt
was ever used for intent classification. The examples below quote real participant requests verbatim, and
`code/tests/test_config_prompts.py` checks two things: that the classifier returns the label shown
for every example, and (when the dataset is available) that each quoted text is the request text of
the case named.

## Decision order

The request text is split into sentences. Then:

1. **Non-deletion scope wins.** If any sentence scopes the request away from deletion, the request
   is not a deletion. Examples are explicit negation ("not a request to erase it", "do not close",
   "don't delete"), keeping the account ("leave the account active", "keep the account as it is",
   "continue using", "still need"), and "only" a narrower action ("marketing lists only"). Explicit
   negation is also covered in Spanish, Portuguese, French, Japanese and Korean. The "keep the
   account" and "only" patterns are English only.
2. **Otherwise, an erasure action on a target authorizes deletion.** At least one sentence must
   contain both an erasure verb (for example delete, erase, remove, purge, close … account; eliminar, borrar,
   supresión; excluir, apagar; supprimer, suppression; 削除, 抹消; 삭제, 탈퇴) and a target (for example account,
   profile, personal data, records, search results, export file, copies, documents; cuenta, conta,
   compte, perfil, profil, datos, dados, données, アカウント, プロフィール, 계정, 프로필). The full lists are
   `ERASURE` and `TARGET` in `intent.py`.
3. **Otherwise, a narrower action is not a deletion.** This covers marketing opt-out, an access
   request, an export or a copy, a correction, a login reset or an invoice.
4. **Anything else is not a deletion.** Unknown or ambiguous text is never treated as
   authorization.

A bare keyword never fires on its own. "Please remove me from marketing lists only" contains
"remove", but it is not a deletion: rule 1 matches "marketing lists only", and the sentence names no
target.

## Examples (verbatim participant requests)

| Case | Label | Request text |
|---|---|---|
| `case_539` | not deletion | Can you tell me what personal information you hold about me? This is an information request, not a request to erase it. |
| `case_538` | not deletion | Please remove me from marketing lists only. I still want receipts and service messages for this account. |
| `case_537` | not deletion | My surname is misspelled on the account. Can you correct it without removing any of my records? |
| `case_542` | not deletion | Could you send me a copy of the information held on my account? I still use it, so please don't delete or change anything. |
| `case_536` | not deletion | I can't access my profile and need help resetting the login. Do not close the account. |
| `case_17` | deletion | I want to close this account permanently. You may retain backups if required, but they must not be able to restore my personal profile. |
| `case_02` | deletion | Intenté eliminar mi cuenta ayer, pero la página terminó mostrando un error. ¿Pueden comprobar si todavía queda algún dato y completar la eliminación si hace falta? |
| `case_03` | deletion | Je demande la suppression définitive du compte associé à cette adresse e-mail. Veuillez examiner les identifiants actuels et antérieurs conformément à votre procédure de confidentialité. Merci de confirmer le résultat une fois la vérification terminée. |
| `case_54` | deletion | Solicito a exclusão permanente da conta associada a este e-mail. Verifiquem os identificadores atuais e anteriores conforme o processo de privacidade. Por favor, confirmem o resultado quando a análise terminar. |
| `case_14` | deletion | このメールアドレスに関連するアカウントの完全な削除を依頼します。 プライバシー手続きに従い、現在および過去の識別情報を確認してください。 確認が完了したら結果をお知らせください。 |
| `case_66` | deletion | 이 이메일 주소에 연결된 계정의 영구 삭제를 요청합니다. 개인정보 처리 절차에 따라 현재 및 이전 식별자를 검토해 주세요. 검토가 완료되면 결과를 확인해 주세요. |

## Coverage on the participant data

Across the 550 participant requests (398 distinct texts), 10 are classified as not authorizing
deletion: `case_533` to `case_542`. Each of those states a narrower action plus an explicit
non-deletion scope. The other 540 express an erasure action on the account or personal data. A few
of these ask to confirm or finish an earlier deletion (for example `case_28`, "Can you verify that
my account was deleted?"), and they are treated as deletion requests. All 32 public answers are
`COMPLETE` or `INCOMPLETE`, so the public sample does not test the `NOT_APPLICABLE` branch. These labels were checked by reading every distinct text, not against gold.
