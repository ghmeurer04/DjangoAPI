"""Rede de segurança do enriquecimento em background.

A thread disparada na importação não sobrevive a um restart do container nem a
uma indisponibilidade prolongada da BrasilAPI. Este comando varre o banco e
reprocessa o que ficou para trás; pode ir para um cron.

Roda de forma síncrona — aqui o próprio comando é o processo de trabalho.
"""

from django.core.management.base import BaseCommand

from seguradoras.services import enriquecer_pendentes


class Command(BaseCommand):
    help = (
        "Enriquece seguradoras pendentes ou com erro consultando a BrasilAPI, "
        "preenchendo nome_fantasia e situacao_cadastral."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--limite",
            type=int,
            default=None,
            help="Processa no máximo esta quantidade de registros.",
        )
        parser.add_argument(
            "--forcar",
            action="store_true",
            help=(
                "Reprocessa também os já concluídos e os não encontrados, "
                "em vez de apenas os pendentes e os com erro."
            ),
        )

    def handle(self, *args, **opcoes):
        resumo = enriquecer_pendentes(
            limite=opcoes["limite"],
            forcar=opcoes["forcar"],
        )

        if not resumo.processados:
            self.stdout.write(self.style.WARNING("Nenhuma seguradora a enriquecer."))
            return

        self.stdout.write(f"Processadas:     {resumo.processados}")
        self.stdout.write(self.style.SUCCESS(f"Concluídas:      {resumo.concluidos}"))

        # Separados porque só um dos dois volta na próxima execução.
        if resumo.nao_encontrados:
            self.stdout.write(
                self.style.WARNING(f"Não encontradas: {resumo.nao_encontrados}")
            )
        if resumo.erros:
            self.stdout.write(
                self.style.ERROR(
                    f"Com erro:        {resumo.erros} "
                    "(serão tentadas de novo na próxima execução)"
                )
            )
