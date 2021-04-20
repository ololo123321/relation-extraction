# relation-extraction
TODO:
* [x] если loss_coef по какому-то таску ровно ноль, то не учитывать скор по этому таску в усреднённом скоре
* [ ] написать скрипты по обучению и инференсу на разных датасетах
* [ ] положить некоторые ноутбуки под гит
* [x] инференс на спанах с перекрытиями
* [ ] базовые тесты моделей (с замоканными экстракторами)
* [ ] оценка качества end2end
* [ ] train, validation, test в докерах
* [ ] тесты функций из src.model.utils
* [x] качество мерить на уровне документов, а не кусочков.  
Пусть:
* w - размер кусочка (в предложениях);
* r(a, b) - отношение между сущностями a и b;
* i - индекс предложения сущности a;
* j - индекс предложения сущности b.  
Тогда в кусочках не будет таких r, что abs(i - j) >= w.  
* [ ] удалить папку bin
* [ ] возможность создавать инстансы класса Example из сырого текста (без файлов .ann)
* [ ] хранить в model_dir vocab.txt (для bpe токенизации)
* [x] feed_dict строить в зависимости от мода, который может принимать три значения: {"train", "valid", "test"}: 
"train" - очевидно, 
"valid" - нужны лейблы для лоса, но не нужны дропауты,
"test" - истинные лейблы неизвестны, дропауты не нужны 
* [ ] - реалзиовать случай window > 1 в инференсе всех моделей (пока сделано только для BertForCoreferenceResolutionV2)

* [x] постпроцессинг предиктов модели BertForCoreferenceResolutionV2:
* петли (head != dep);
* циклы (удалять стрелки "->" в случае циклов);
* несколько исходящих рёбер (использовать логиты);
* несколько входящих рёбер (хз). 
* [ ] получение компонент связности (+ присовение атрибуты сущности id_chain)
* [x] метрики для coreference resolution (чистый код сохранения в conll)
* [ ] нарисовать в draw.io процесс обучения и инференса (становится сложна все этапы в голове держать)

Идеи для улучшения модели разрешения кореференций:
* [ ] увеличить ширину окна 
* [ ] увеличить куски до максимально возможного размера (512 bpe-pieces). тогда скорей всего придётся учить с аккумуляцией градиентов (в случае берта).
* [ ] более умная кластеризация. например, если есть большая компонента связности, 
которую никак не ухватить моделью, и модель её разбила на k компонент, 
то можно их попробовать объединить по группам существительного. 
Например, интуитивно понятно, что компоненты {"Иван Иванов", "он"}, {"Ивану"} можно объединить в одну: {"Иван Иванов", "он", "Ивану"}.
* [ ] markov clustering: https://micans.org/mcl/. наверное, это лучше пробовать на логитах модели, которая училась на таргете, описанном в пунтке ниже.
* [ ] попробовать поменять таргет: label(i, j) = {1, если сущности i и j принадлежат одной компоненте, 0 - иначе}
* [ ] linking pairs -> agglomerative clustering (https://arxiv.org/pdf/1606.01323.pdf)