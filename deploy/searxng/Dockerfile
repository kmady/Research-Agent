# Image SearXNG de Research-Agent : la configuration est intégrée à l'image.
# Plus de volume monté ni de « docker cp » : modifier settings.yml = nouvelle image, testée en CI.
FROM searxng/searxng:2026.11.17-b99cf7496@sha256:0b9f3ddd85efc8df189e6543d65702abfa8f3ed1c32229a53f7707cd12b554ef

COPY --chown=searxng:searxng settings.yml /etc/searxng/settings.yml
