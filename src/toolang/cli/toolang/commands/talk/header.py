"""Talk's startup identity snapshot."""

from toolang.cli.common.banner import Banner, version_label
from toolang.teaming.schemas import ConversationView, target


def startup_header(
    *, client_version: str, hub_version: str, conversation: ConversationView, human: str
) -> Banner:
    user = target(human).name
    if not conversation.allows_sender(human):
        user += " · view only"
    return Banner(
        caption=f"Talk {version_label(client_version)}",
        fields=(
            ("hub", version_label(hub_version)),
            ("convo", conversation.id),
            ("user", user),
        ),
    )
