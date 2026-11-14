# 14/11/2026 · provider-ops 1.1.1

## Correção urgente dos avisos

- Reset é arredondado à hora mais próxima somente na chave de dedupe.
- Cada conta/janela avisa apenas o maior limiar cruzado; 85→95 pode gerar um segundo aviso, uma única vez.
- Estado e lock são compartilhados no home raiz da instalação.
- Manu é a única emissora. O hook exige `/opt/manuella/provider-ops/alerts-owner` contendo exatamente `manu`. Luna e outras instalações não enviam alertas.
- A migração preserva IDs reais do histórico. Avisos antigos são reconhecidos com deriva de reset de até uma hora.
- Fixture local, sem rede nem Telegram, cobre jitter, limiares, perfis compartilhados, migração e emissora única.

## Rollout: PARTIAL, não liberar o tanque

- Manu/Luna já estavam no Hermes 0.23.1. A correção do plugin é independente do core.
- Aquário continua 0.23.0: a preparação de plugin falhou antes do corte; preimagem restaurada. Hooks residuais de provider-ops carregados durante a preparação foram retirados com reload nativo, sem reiniciar gateways.
- Mafe continua 0.22.7: 0.23.1 passou versão/readiness, mas o aceite API retornou ConnectionRefusedError111; rollback automático.
- O webhook do bot retornou200, mas refresh falhou na ponte. Apenas a mensagem TESTE51 foi criada. Não há prova de destravamento no gateway vivo.
- Os adaptadores de rollout têm testes herméticos, mas NÃO têm aceite completo de produção. O comando do tanque é candidato, não autorização para executar.
- Nenhum arquivo de outro plugin, modelo, prioridade ou segredo foi alterado. Nenhuma alteração foi executada no tanque.

Evidências privadas: workspace da Carla, `provider-ops-stage2-evidence/`.
