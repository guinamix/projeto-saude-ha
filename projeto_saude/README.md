# Projeto Saúde

Acompanhamento de saúde dentro do Home Assistant: peso e bioimpedância, caminhadas e passos do Apple Watch, sono com fases, tratamento com Mounjaro (opcional), metas e relatório para o médico.

- Cada usuário do Home Assistant tem o seu próprio painel e os seus próprios dados.
- Os atalhos do iPhone enviam os dados para a porta 8765 (`POST /i/<token>`). O endereço de cada usuário aparece no app, em **Conexão com o iPhone**.
- Os dados ficam em `/data/saude.db` (incluído nos backups do Home Assistant).
