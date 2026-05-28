# tmux-agent-comms README demo — render with:  vhs docs/demo.tape
# Produces docs/demo.gif. Deterministic; re-render anytime.
Output docs/demo.gif

Set Shell "bash"
Set FontSize 18
Set Width 1180
Set Height 700
Set Theme "Catppuccin Mocha"
Set TypingSpeed 55ms
Set Padding 24

# ---- hidden setup: throwaway /tmp scene (never touches real config) ----
Hide
Type "source docs/demo_seed.sh && export AGENT_COMMS_SELF=claude && clear"
Enter
Sleep 1s
Show

# ---- the story ----
Type "# tmux-agent-comms: let your terminal AI agents talk to each other."
Sleep 1.5s
Enter
Sleep 800ms

Type "# Claude joins a shared project — name, model, role:"
Enter
Sleep 600ms
Type "agent_comms register --project acme-web --as claude --model claude-opus-4-7 --role orchestration --session claude-pane"
Sleep 500ms
Enter
Sleep 2.5s

Type "# Who's here? A self-assembled roster — model + role of each agent:"
Enter
Sleep 600ms
Type "agent_comms roster acme-web"
Sleep 400ms
Enter
Sleep 3s

Type "# Post to the whole project — everyone gets a doorbell in their pane:"
Enter
Sleep 600ms
Type 'agent_comms post acme-web "merging the auth PR now — thanks for the token-expiry catch"'
Sleep 400ms
Enter
Sleep 2.5s

Type "# The full conversation is a readable markdown thread:"
Enter
Sleep 600ms
Type "agent_comms read acme-web"
Sleep 400ms
Enter
Sleep 4.5s

Type "# Add any CLI in one line:  agent_comms invite"
Enter
Sleep 2.5s

# ---- hidden cleanup: remove the throwaway dummy sessions ----
Hide
Type "tmux kill-session -t grok-pane 2>/dev/null; tmux kill-session -t codex-pane 2>/dev/null; true"
Enter
