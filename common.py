import os
import yaml
from Bio import SeqIO
import gzip
from lcdblib.utils.imports import resolve_name
from lcdblib.snakemake import aligners, helpers
from snakemake.shell import shell


def cat(tmpfiles, outfile):
    shell('cat {tmpfiles} > {outfile}')


def filter_fastas(tmpfiles, outfile, pattern):
    """
    Given input FASTAs, create a new one containing only records whose
    description matches `pattern`.

    Parameters
    ----------
    tmpfiles : list
        gzipped fasta files to look through

    outfile : str
        gzipped output fastq file

    pattern : str
        Look for this string in each record's description

    """
    def gen():
        for tmp in tmpfiles:
            handle = gzip.open(tmp, 'rt')
            parser = SeqIO.parse(handle, 'fasta')
            for rec in parser:
                if pattern not in rec.description:
                    continue
                rec.seq = rec.seq.back_transcribe()
                rec.description = rec.name
                yield rec

    with gzip.open(outfile, 'wt') as fout:
        SeqIO.write(gen(), fout, 'fasta')


def download_and_postprocess(outfile, config, assembly, tag):
    """
    Given an output file, figure out what to do based on the config.

    This function:

        - parses the assembly and tag from the `outfile`
        - uses that as a key into the config dict to figure out:
            - what postprocessing function (if any) was specified along with
              its optional args
            - the URL[s] to download
        - resolves the name of that function and imports it
        - downloads the URL[s] to tempfile[s]
        - calls the imported function using the tempfile[s] and outfile plus
          any additional specified arguments.

    If defined, the function must assume a list of input gzipped files and must
    create the gzipped output file (whose name is given as its 2nd input arg).
    """
    # Build a lookup dict of the config file keyed by (assembly, tag). Make sure
    # all references have a tag, using "default" if none specified.
    d = {}
    for i in config['references']:
        k = (i['assembly'], i.get('tag', 'default'))
        if k in d:
            raise ValueError("key {} already exists".format(k))
        d[k] = i


    def default_postprocess(origfn, newfn):
        """
        If no other postprocess function is defined, then simply move the original
        to the new.
        """
        shell("mv {origfn} {newfn}")

    base = os.path.relpath(outfile, config['references_dir'])
    basename = os.path.basename(outfile)

    block = d[(assembly, tag)]

    # postprocess can be missing, in which case we use the default above
    post_process = block.get('postprocess', None)
    if post_process is None:
        func = default_postprocess
        args = ()

    # postprocess can have a single string value (indicating the function) or
    # it can be a dict with keys "function" and optionally "args". The value of
    # "args" can be a string or a list.
    else:
        if isinstance(post_process, dict):
            name = post_process.get('function', post_process)
            args = post_process.get('args', ())
            if isinstance(args, str):
                args = (args,)
        elif isinstance(post_process, str):
            name = post_process
            args = ()

        # import the function
        func = resolve_name(name)

    # functions assume a list of urls
    urls = block['url']
    if isinstance(urls, str):
        urls = [urls]

    tmpfiles = ['{0}.{1}.tmp'.format(outfile, i) for i in range(len(urls))]

    for url, tmpfile in zip(urls, tmpfiles):
        shell("wget {url} -O- > {tmpfile} 2> {outfile}.log")

    func(tmpfiles, outfile, *args)

    for i in tmpfiles:
        if os.path.exists(i):
            shell('rm {i}')


def config_to_dict(config):
    """
    The config file is designed to be easy to edit and use from the user's
    standpoint. But it's not so great for practical usage. Here we convert the
    config file which has the format::

    >>> from textwrap import dedent
    >>> fout = open('tmp', 'w')
    >>> _ = fout.write(dedent('''
    ... references_dir: "/data"
    ... references:
    ...   -
    ...     assembly: dm6
    ...     tag: "r6-11"
    ...     url: ""
    ...     type: fasta
    ...     indexes:
    ...         - bowtie2
    ...         - hisat2
    ...   -
    ...     assembly: dm6
    ...     tag: "r6-11_transcriptome"
    ...     url: ""
    ...     type: fasta
    ...     indexes:
    ...         - kallisto
    ...   -
    ...     assembly: dm6
    ...     type: gtf
    ...     url: ""
    ...     conversions:
    ...       - refflat
    ... '''))
    >>> fout.close()

    To this format:

    >>> d = config_to_dict('tmp')
    >>> assert d == (
    ... {
    ...   'dm6': {
    ...      'r6-11': {
    ...          'fasta': '/data/dm6/fasta/dm6_r6-11.fasta',
    ...          'chromsizes': '/data/dm6/fasta/dm6_r6-11.chromsizes',
    ...          'bowtie2': '/data/dm6/bowtie2/dm6_r6-11.1.bt2',
    ...          'hisat2': '/data/dm6/hisat2/dm6_r6-11.1.ht2',
    ...          },
    ...      'r6-11_transcriptome': {
    ...          'fasta': '/data/dm6/fasta/dm6_r6-11_transcriptome.fasta',
    ...          'chromsizes': '/data/dm6/fasta/dm6_r6-11_transcriptome.chromsizes',
    ...          'kallisto': '/data/dm6/kallisto/dm6_r6-11_transcriptome.idx',
    ...          },
    ...      'default': {
    ...          'gtf': '/data/dm6/gtf/dm6_default.gtf',
    ...          'refflat': '/data/dm6/gtf/dm6_default.refflat',
    ...       },
    ...     },
    ... }), d
    >>> os.unlink('tmp')

    With this new dictionary, other parts of the config or snakemake rules can
    access the files by d[assembly][tag][type].
    """
    if isinstance(config, str):
        config = yaml.load(open(config))

    references_dir = os.environ.get('REFERENCES_DIR', config.get('references_dir', None))
    if references_dir is None:
        raise ValueError('References dir not set')

    # Map "indexes" value to a pattern specific to each index.
    index_extensions = {
        'bowtie2': aligners.bowtie2_index_from_prefix('')[0],
        'hisat2': aligners.hisat2_index_from_prefix('')[0],
        'kallisto': '.idx',
    }


    conversion_extensions = {
        'intergenic': '.intergenic.gtf',
        'refflat': '.refflat',
    }

    d = {}
    for block in config['references']:
        assembly = block['assembly']
        tag = block.get('tag', 'default')
        type_ = block['type']

        if assembly not in d:
            d[assembly] = {}

        e = d[assembly].get(tag, {})

        if type_ in e:
            raise ValueError(
                "tag {tag} already exists for type {type_} in assembly {assembly}"
                .format(**locals())
            )

        e[type_] = (
            '{references_dir}/'
            '{assembly}/'
            '{type_}/'
            '{assembly}_{tag}.{type_}'.format(**locals())
        )

        # Add conversions if specified.
        if block['type'] == 'gtf':
            conversions = block.get('conversions', [])
            for conversion in conversions:
                ext = conversion_extensions[conversion]
                e[conversion] = (
                    '{references_dir}/'
                    '{assembly}/'
                    '{type_}/'
                    '{assembly}_{tag}{ext}'.format(**locals())
                )

        if block['type'] == 'fasta':
            # Add indexes if specified
            indexes = block.get('indexes', [])
            for index in indexes:
                ext = index_extensions[index]

                e[index] = (
                    '{references_dir}/{assembly}/{index}/{assembly}_{tag}{ext}'
                    .format(**locals())
                )

            e['chromsizes'] = (
                '{references_dir}/'
                '{assembly}/'
                '{type_}/'
                '{assembly}_{tag}.chromsizes'.format(**locals())
            )
        d[assembly][tag] = e

    return d
